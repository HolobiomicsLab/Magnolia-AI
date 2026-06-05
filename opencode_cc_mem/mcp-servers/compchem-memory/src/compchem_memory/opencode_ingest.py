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
    staging = store / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    title = candidate.get("title", "untitled")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:60].strip("_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    fpath = staging / f"{ts}_{slug}.md"
    now = datetime.now(timezone.utc).isoformat()
    fm = {
        "id": ts,
        "type": candidate.get("type", "note"),
        "title": title,
        "description": candidate.get("content", "")[:200],
        "tools": candidate.get("tools", []),
        "tags": candidate.get("tags", []),
        "created": now,
        "updated": now,
        "source": "opencode_distill",
        "opencode_session_id": ses_id,
        "observation_count": 1,
        "confidence": candidate.get("confidence", 0.5),
    }
    fpath.write_text("---\n" + yaml.dump(fm, default_flow_style=False) + "---\n\n"
                     + candidate.get("content", "") + "\n")
    return str(fpath)


def _default_distiller(transcript: str) -> list[dict[str, Any]]:
    from compchem_memory.extraction import AutomaticMemoryExtractor
    return AutomaticMemoryExtractor().distill_transcript(transcript)


def ingest_opencode_sessions(
    store_dir: str,
    mapping_path: str | None = None,
    *,
    exporter: Callable[[str], Optional[dict]] = export_session,
    distiller: Callable[[str], list[dict[str, Any]]] = _default_distiller,
) -> list[str]:
    """Distill every not-yet-processed opencode session in the mapping. Returns
    the saved staging-entry paths. `exporter`/`distiller` are injectable for tests."""
    store = Path(store_dir)
    mapping = Path(mapping_path) if mapping_path else store / "opencode-sessions.jsonl"
    markers = store / "opencode-distilled"
    markers.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for sid in _read_mapping_ids(mapping):
        if (markers / f"{sid}.json").exists():
            continue
        export = exporter(sid)
        if export is None:
            continue  # export failed — don't mark; retry on a later sweep
        transcript = scrub_secrets(reconstruct_transcript(export))
        candidates = distiller(transcript) if transcript.strip() else []
        if candidates is None:
            # Distillation FAILED (LLM error / context overflow), as opposed to
            # succeeding with nothing to extract ([]). Don't mark — retry on a
            # later sweep so a transient failure never loses the session.
            continue
        for c in candidates:
            if isinstance(c, dict) and c.get("title"):
                saved.append(_save_candidate(store, c, sid))
        (markers / f"{sid}.json").write_text(
            json.dumps({
                "distilled_at": datetime.now(timezone.utc).isoformat(),
                "candidates": len(candidates),
            }) + "\n"
        )
    return saved
