"""Ingest real opencode conversation transcripts into magnolia distillation.

The capture plugin (.opencode/plugins/magnolia-session-capture.ts) writes
`<store>/opencode-sessions.jsonl` mapping each opencode `ses_<id>` to the
project. This module turns those ids into distilled learnings:

  read mapping -> for each ses_id WITHOUT a marker:
    `opencode export <id>` (raw) -> reconstruct transcript -> scrub secrets
    -> distill (conversation-oriented) -> save candidates to staging -> mark done

The per-ses_id marker (mirrors scan_and_distill's `.distilled` markers) gives
once-only processing, so we never re-export the whole history.

NOTE on secrets: `opencode export --sanitize` redacts the transcript *content*,
not just secrets, so it is unusable. We export raw and scrub known key shapes
here before any content reaches the (external) distillation LLM.
"""

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml


# Conservative secret shapes — clear keys/tokens, not ordinary prose.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),          # long hex (hashes/tokens)
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # long base64-ish blobs
]


def scrub_secrets(text: str) -> str:
    """Redact known secret shapes before content reaches an external LLM."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED_SECRET]", out)
    return out


def reconstruct_transcript(export: dict) -> str:
    """Build an ordered text transcript from `opencode export` JSON.

    Export shape: {info, messages:[{info:{role}, parts:[{type,text}]}]}.
    Keeps user/assistant text and assistant reasoning (the scientific content);
    tool parts are noted briefly.
    """
    lines: list[str] = []
    for m in export.get("messages", []) or []:
        role = ((m.get("info") or {}).get("role") or "?").upper()
        for p in m.get("parts", []) or []:
            t = p.get("type")
            txt = p.get("text")
            if t == "text" and txt:
                lines.append(f"{role}: {txt}")
            elif t == "reasoning" and txt:
                lines.append(f"{role} (reasoning): {txt}")
            elif t == "tool":
                name = p.get("tool") or p.get("name") or "tool"
                lines.append(f"{role} (tool:{name})")
    return "\n\n".join(lines)


def export_session(ses_id: str) -> Optional[dict]:
    """`opencode export <id>` raw (NOT --sanitize). Returns parsed JSON or None
    on any failure, so the caller can retry on a later sweep rather than marking
    a failed export as done.

    IMPORTANT: `opencode export` truncates its stdout at one 64 KB pipe buffer
    when stdout is a pipe — so `capture_output=True` silently yields invalid
    (cut-off) JSON for any session larger than 64 KB, which are exactly the
    content-rich sessions worth distilling. Writing to a regular FILE gets the
    full output, so we redirect to a temp file and read it back."""
    import os
    import tempfile

    try:
        fd, tmp = tempfile.mkstemp(prefix="oc_export_", suffix=".json")
        os.close(fd)
        try:
            with open(tmp, "w") as out:
                proc = subprocess.run(
                    ["opencode", "export", ses_id],
                    stdout=out, stderr=subprocess.DEVNULL, timeout=120,
                )
            if proc.returncode != 0:
                return None
            data = Path(tmp).read_text()
            if not data.strip():
                return None
            return json.loads(data)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        return None


def _read_mapping_ids(mapping: Path) -> list[str]:
    if not mapping.exists():
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for line in mapping.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sid = json.loads(line).get("opencode_session_id")
        except json.JSONDecodeError:
            continue
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids


def _save_candidate(store: Path, candidate: dict, ses_id: str) -> str:
    """Write a distilled candidate to staging, stamped with provenance back to
    the opencode session it came from."""
    from compchem_memory.staging_io import save_candidate

    return save_candidate(store, candidate, source="opencode_distill",
                          opencode_session_id=ses_id)


def _default_distiller(transcript: str) -> list[dict[str, Any]]:
    from compchem_memory.extraction import AutomaticMemoryExtractor
    return AutomaticMemoryExtractor().distill_transcript(transcript)


def _latest_sid(mapping: Path) -> str | None:
    """The active session = the most recently appended id in the mapping. The
    capture plugin appends the live session last, so the final non-empty line
    names it."""
    if not mapping.exists():
        return None
    latest = None
    for line in mapping.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sid = json.loads(line).get("opencode_session_id")
        except json.JSONDecodeError:
            continue
        if sid:
            latest = sid
    return latest


def _messages_after_cursor(messages: list[dict], cursor: str | None) -> list[dict]:
    """Messages appended after the one whose info.id == cursor. No cursor — or a
    cursor not present in this export — yields all messages: the durable export is
    append-only, so a missing cursor means 'distil from the start', never stall.
    Matching on message id (not index) is robust to compaction injecting a
    summary message and shifting positions."""
    if not cursor:
        return list(messages)
    for i, m in enumerate(messages):
        if (m.get("info") or {}).get("id") == cursor:
            return messages[i + 1:]
    return list(messages)


def _last_message_id(messages: list[dict]) -> str | None:
    for m in reversed(messages):
        mid = (m.get("info") or {}).get("id")
        if mid:
            return mid
    return None


def _read_progress(rec_path: Path) -> dict | None:
    try:
        return json.loads(rec_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_progress(rec_path: Path, cursor: str | None, candidates: int, done: bool) -> None:
    rec_path.write_text(json.dumps({
        "cursor": cursor,
        "candidates": candidates,
        "done": done,
        "updated": datetime.now(timezone.utc).isoformat(),
    }) + "\n")


def distill_session_transcript(
    store_dir: str,
    sid: str,
    *,
    commit: bool,
    exporter: Callable[[str], Optional[dict]] | None = None,
    distiller: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Distil ONE session's new (post-cursor) dialogue, for the manual tool.

    ``commit=False`` previews: returns the candidates, saves nothing, leaves the
    cursor untouched. ``commit=True`` saves to staging and advances the cursor
    (forced — no threshold gate). Returns ``None`` if the transcript is
    unavailable (export failed), so the caller can fall back. ``exporter`` /
    ``distiller`` resolve to the live functions when not injected, so the module
    attributes stay monkeypatchable.
    """
    exporter = exporter or export_session
    distiller = distiller or _default_distiller

    store = Path(store_dir)
    rec_path = store / "opencode-distilled" / f"{sid}.json"
    rec = _read_progress(rec_path)
    cursor = rec.get("cursor") if rec else None

    export = exporter(sid)
    if export is None:
        return None  # unavailable — let caller fall back

    new_msgs = _messages_after_cursor(export.get("messages") or [], cursor)
    transcript = scrub_secrets(reconstruct_transcript({"messages": new_msgs}))
    candidates = distiller(transcript) if transcript.strip() else []
    if candidates is None:
        candidates = []
    kept = [c for c in candidates if isinstance(c, dict) and c.get("title")]

    if not commit:
        return {"status": "preview", "candidates": kept}

    rec_path.parent.mkdir(parents=True, exist_ok=True)
    saved = [_save_candidate(store, c, sid) for c in kept]
    new_cursor = _last_message_id(new_msgs) or cursor
    prior = rec.get("candidates", 0) if rec else 0
    _write_progress(rec_path, new_cursor, prior + len(kept), done=False)
    return {"status": "committed", "saved": saved}


def ingest_opencode_sessions(
    store_dir: str,
    mapping_path: str | None = None,
    *,
    exporter: Callable[[str], Optional[dict]] = export_session,
    distiller: Callable[[str], list[dict[str, Any]]] = _default_distiller,
) -> list[str]:
    """Incrementally distil opencode conversations into staging.

    Cursor-based, not once-and-done: each sweep distils only the messages
    appended since the recorded cursor. The active session (latest in the
    mapping) is re-swept every call and never sealed, so a long live session
    keeps producing learnings as it grows. A superseded session is sealed
    (``done: true``) once drained and skipped thereafter — that is the only state
    that stops re-export. Returns saved staging-entry paths. ``exporter`` /
    ``distiller`` are injectable for tests.

    The per-session record at ``opencode-distilled/<sid>.json`` is a progress
    record: ``{cursor, candidates, done, updated}``.
    """
    store = Path(store_dir)
    mapping = Path(mapping_path) if mapping_path else store / "opencode-sessions.jsonl"
    markers = store / "opencode-distilled"
    markers.mkdir(parents=True, exist_ok=True)

    latest = _latest_sid(mapping)
    saved: list[str] = []

    for sid in _read_mapping_ids(mapping):
        rec_path = markers / f"{sid}.json"
        rec = _read_progress(rec_path)
        if rec and rec.get("done"):
            continue  # sealed — never re-export

        cursor = rec.get("cursor") if rec else None
        prior_count = rec.get("candidates", 0) if rec else 0
        is_active = sid == latest

        export = exporter(sid)
        if export is None:
            continue  # export failed — don't advance; retry on a later sweep

        new_msgs = _messages_after_cursor(export.get("messages") or [], cursor)
        if not new_msgs:
            # Nothing new since the cursor. Seal it if it is no longer the active
            # session, so we stop re-exporting a finished conversation; leave the
            # active one open for its next slice.
            if not is_active:
                _write_progress(rec_path, cursor, prior_count, done=True)
            continue

        transcript = scrub_secrets(reconstruct_transcript({"messages": new_msgs}))
        candidates = distiller(transcript) if transcript.strip() else []
        if candidates is None:
            # Distillation FAILED (LLM error / overflow), distinct from succeeding
            # with nothing ([]). Don't advance the cursor — retry next sweep.
            continue

        kept = [c for c in candidates if isinstance(c, dict) and c.get("title")]
        for c in kept:
            saved.append(_save_candidate(store, c, sid))

        new_cursor = _last_message_id(new_msgs) or cursor
        _write_progress(rec_path, new_cursor, prior_count + len(kept), done=not is_active)

    return saved
