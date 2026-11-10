"""Dreaming pass (prototype): re-check memory entries against their transcripts.

Origin: the 2026-11-09 discussion of Anthropic's "The components of agent
memory". Distillation writes memory forward, in-session; dreaming reads it
backward, offline, and checks it against the session transcript that produced
it. A note that was wrong from the start, or that drifted, can then be
corrected before a later session trusts it.

Scope of this prototype (deliberately narrow):

- entries with a transcript anchor only (``opencode_session_id`` and/or
  ``observed_in_sessions``); entries without one are skipped, not guessed at;
- bounded candidate selection (``max_entries``) and bounded apply
  (``max_apply``);
- an LLM judge proposes a verdict, deterministic code applies it — the model
  never writes the store;
- every mutation re-reads the entry and checks the proposal-time content hash,
  so a proposal never lands on an entry a human edited in the meantime;
- corrections are append-only: a ``## Dreaming correction`` section is added to
  the entry body, plus a ``dreaming_status``/``dreaming_checked_at`` stamp in
  frontmatter. Nothing is silently rewritten or deleted;
- every processed proposal leaves a receipt in ``reflex/dreaming-log.jsonl``
  and entry changes are committed to the store's versioning repo.

This is NOT an integrity check. If the agent can edit its own transcripts, the
"ground truth" dreaming validates against is forgeable (see the trace-tampering
digest). Treat verdicts as consistency findings until transcripts are
append-only or hash-chained.

PROVISIONAL: artifact shape and verdict names may change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .atomic_io import atomic_write_text
from .reflex_common import parse_frontmatter_file, pending_indices

VERDICTS = ("confirmed", "stale", "wrong", "unverifiable")
PROPOSAL_FILE = "dreaming-proposal.json"
LOG_FILE = "dreaming-log.jsonl"


@dataclass(frozen=True)
class DreamingConfig:
    """Bounds for one dreaming pass. The task-pack protocol carries the same
    fields so an approval covers the exact scope that will execute."""

    max_entries: int = 20
    max_sessions_per_entry: int = 3
    max_transcript_chars: int = 60_000
    max_entry_chars: int = 12_000
    max_apply: int = 10
    recheck_days: int = 7
    commit: bool = True
    tiers: tuple[str, ...] = ("staging", "entries")

    @classmethod
    def from_protocol(cls, protocol: dict[str, Any]) -> "DreamingConfig":
        bounds = protocol.get("bounds", {}) or {}
        kwargs: dict[str, Any] = {}
        for field in (
            "max_entries",
            "max_sessions_per_entry",
            "max_transcript_chars",
            "max_entry_chars",
            "max_apply",
            "recheck_days",
        ):
            if field in bounds:
                kwargs[field] = int(bounds[field])
        return cls(**kwargs)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _session_ids(meta: dict[str, Any]) -> list[str]:
    """All transcript anchors for an entry, deduped, in a stable order."""
    ids: list[str] = []
    sid = meta.get("opencode_session_id")
    if sid:
        ids.append(str(sid))
    for s in meta.get("observed_in_sessions") or []:
        if s and str(s) not in ids:
            ids.append(str(s))
    return ids


def _recently_checked(meta: dict[str, Any], config: DreamingConfig) -> bool:
    if config.recheck_days <= 0:
        return False
    raw = meta.get("dreaming_checked_at")
    if not raw:
        return False
    try:
        checked = datetime.fromisoformat(str(raw)[:19])
    except (ValueError, TypeError):
        return False
    checked = checked if checked.tzinfo else checked.replace(tzinfo=timezone.utc)
    return _now() - checked < timedelta(days=config.recheck_days)


def select_dirty_entries(store: Path, config: DreamingConfig) -> list[dict[str, Any]]:
    """Deterministic candidate selection, in tier order then filename order.

    Only entries with a transcript anchor are eligible: dreaming without a
    transcript would be the model grading its own recollection. Parked entries
    and entries checked within ``recheck_days`` are skipped.
    """
    store = Path(store)
    out: list[dict[str, Any]] = []
    for tier in config.tiers:
        tier_dir = store / tier
        if not tier_dir.is_dir():
            continue
        for path in sorted(tier_dir.glob("*.md")):
            parsed = parse_frontmatter_file(path)
            if parsed is None:
                continue
            meta = parsed["meta"] or {}
            if meta.get("parked"):
                continue
            sessions = _session_ids(meta)
            if not sessions:
                continue
            if _recently_checked(meta, config):
                continue
            out.append({
                "entry": parsed["id"],
                "path": str(path),
                "tier": tier,
                "meta": meta,
                "body": parsed["body"][: config.max_entry_chars],
                "sessions": sessions,
                "sha256": _sha256(path),
            })
            if len(out) >= config.max_entries:
                return out
    return out


def _render_session_jsonl(path: Path, limit: int) -> str:
    """Compact rendering of a SessionManager JSONL log (one line per event)."""
    lines: list[str] = []
    used = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        parts = [str(row.get("timestamp", "")), str(row.get("event_type", ""))]
        for key in ("tool", "args_summary", "result_summary", "error"):
            val = row.get(key)
            if val:
                parts.append(f"{key}={str(val)[:600]}")
        line = " | ".join(p for p in parts if p)
        if not line.strip():
            continue
        lines.append(line)
        used += len(line)
        if used >= limit:
            break
    return "\n".join(lines)[:limit]


def _render_opencode_json(path: Path, limit: int) -> str:
    """Render an exported opencode transcript (info/parts message shape)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(data, list):
        return json.dumps(data)[:limit]
    out: list[str] = []
    used = 0
    for msg in data:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info") or {}
        role = info.get("role", "?")
        chunks: list[str] = []
        for part in msg.get("parts") or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                chunks.append(str(part.get("text", "")))
            elif ptype == "tool":
                name = part.get("tool") or part.get("name") or "tool"
                state = part.get("state") or {}
                result = state.get("output") or state.get("error") or ""
                chunks.append(f"[{name}] {str(result)[:500]}")
        text = "\n".join(c for c in chunks if c).strip()
        if not text:
            continue
        block = f"[{role}] {text}"
        out.append(block)
        used += len(block)
        if used >= limit:
            break
    return "\n".join(out)[:limit]


def resolve_transcripts(
    project_dir: str | Path, sessions: list[str], config: DreamingConfig
) -> dict[str, Any]:
    """Find and render the source transcripts for one entry.

    Sources are looked up in a fixed order per session id: an exported opencode
    transcript first, then the SessionManager JSONL log. Missing sources are
    recorded as None — an entry with no transcript must not be judged.
    """
    store = Path(project_dir) / ".magnolia"
    selected = sessions[: config.max_sessions_per_entry]
    per_source = max(1000, config.max_transcript_chars // max(1, len(selected)))
    sources: dict[str, Any] = {}
    blocks: list[str] = []
    for sid in selected:
        oc = store / "opencode-transcripts" / f"{sid}.json"
        jl = store / "sessions" / f"{sid}.jsonl"
        text = ""
        path_used: str | None = None
        if oc.exists():
            text = _render_opencode_json(oc, per_source)
            path_used = str(oc)
        elif jl.exists():
            text = _render_session_jsonl(jl, per_source)
            path_used = str(jl)
        sources[sid] = {"path": path_used, "chars": len(text)}
        if text:
            blocks.append(f"## session {sid}\n{text}")
    combined = "\n\n".join(blocks)
    return {
        "text": combined[: config.max_transcript_chars],
        "sources": sources,
        "truncated": len(combined) > config.max_transcript_chars,
    }


def _default_judge(entry: dict[str, Any], transcript: str) -> dict[str, Any] | None:
    """LLM verdict on one entry against its transcript. None on any failure —
    a judge outage must never be read as a verdict.

    ``MAGNOLIA_DREAMING_JUDGE=off`` disables the LLM entirely (dry run /
    offline demo): every anchored entry comes back unverifiable and nothing is
    mutated.
    """
    import os
    if (os.environ.get("MAGNOLIA_DREAMING_JUDGE") or "").strip().lower() == "off":
        return None
    from .llm import call_llm_json

    system_prompt = (
        "You audit one agent memory entry against the session transcript that "
        "produced it. Answer with a JSON object only: "
        '{"verdict": "confirmed|stale|wrong|unverifiable", '
        '"corrected_text": "...", "evidence": [{"source": "...", "quote": "..."}], '
        '"rationale": "..."}. '
        "confirmed = the transcript supports the entry. "
        "stale = it was true but the situation has moved on. "
        "wrong = the transcript contradicts it; supply corrected_text and at "
        "least one verbatim evidence quote from the transcript. "
        "unverifiable = the transcript does not settle it. "
        "Never invent transcript content."
    )
    user_content = (
        f"ENTRY TITLE: {entry['meta'].get('title', '')}\n\n"
        f"ENTRY BODY:\n{entry['body'][:12000]}\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )
    raw = call_llm_json(
        system_prompt, user_content, max_tokens=1500,
        temperature=0, disable_thinking=True,
    )
    return raw if isinstance(raw, dict) else None


def _normalize_verdict(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"verdict": "unverifiable", "corrected_text": "", "evidence": [],
                "rationale": "judge unavailable or invalid response"}
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        verdict = "unverifiable"
    evidence = raw.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    return {
        "verdict": verdict,
        "corrected_text": str(raw.get("corrected_text") or "").strip(),
        "evidence": evidence,
        "rationale": str(raw.get("rationale") or "").strip(),
    }


def propose_dreaming(
    project_dir: str | Path,
    config: DreamingConfig | None = None,
    judge: Callable[[dict[str, Any], str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Select candidates, judge each against its transcript, write the artifact.

    The artifact is the unit of review and of crash recovery: it records the
    content hash of every entry at proposal time.
    """
    config = config or DreamingConfig()
    judge = judge or _default_judge
    store = Path(project_dir) / ".magnolia"
    reflex = store / "reflex"
    reflex.mkdir(parents=True, exist_ok=True)

    proposals: list[dict[str, Any]] = []
    for entry in select_dirty_entries(store, config):
        transcripts = resolve_transcripts(project_dir, entry["sessions"], config)
        if not transcripts["text"].strip():
            verdict = {
                "verdict": "unverifiable",
                "corrected_text": "",
                "evidence": [],
                "rationale": "no transcript found for the anchored session(s)",
            }
        else:
            verdict = _normalize_verdict(judge(entry, transcripts["text"]))
        proposals.append({
            "entry": entry["entry"],
            "path": entry["path"],
            "tier": entry["tier"],
            "sha256": entry["sha256"],
            "sessions": entry["sessions"],
            "transcript_sources": transcripts["sources"],
            "transcript_chars": len(transcripts["text"]),
            **verdict,
        })

    artifact = {
        "id": _now().strftime("%Y%m%d_%H%M%S"),
        "created_at": _now().isoformat(),
        "config": asdict(config),
        "proposals": proposals,
        "applied": [],
        "rejected": [],
        "dismissed": [],
    }
    atomic_write_text(reflex / PROPOSAL_FILE, json.dumps(artifact, indent=2))
    return artifact


def _serialize_entry(meta: dict[str, Any], body: str) -> str:
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def _correction_section(proposal: dict[str, Any], checked_at: str) -> str:
    evidence_lines = []
    for item in proposal["evidence"]:
        if isinstance(item, dict):
            src = item.get("source", "")
            quote = str(item.get("quote", "")).strip()
            if quote:
                evidence_lines.append(f"  - {src}: {quote}" if src else f"  - {quote}")
        elif item:
            evidence_lines.append(f"  - {item}")
    evidence = "\n".join(evidence_lines) or "  - (none supplied)"
    sessions = ", ".join(proposal["sessions"])
    return (
        f"\n\n## Dreaming correction ({checked_at[:10]})\n"
        f"- source sessions: {sessions}\n"
        f"- rationale: {proposal['rationale'] or '(none supplied)'}\n"
        f"- evidence:\n{evidence}\n\n"
        f"{proposal['corrected_text']}\n"
    )


def apply_dreaming(
    project_dir: str | Path,
    artifact: dict[str, Any] | None = None,
    config: DreamingConfig | None = None,
) -> dict[str, Any]:
    """Deterministic apply of a proposal artifact.

    Model proposes, code applies. Only three outcomes touch disk: a ``stale``
    frontmatter stamp, an append-only ``wrong`` correction, and the receipt
    log. A ``wrong`` verdict without corrected_text or evidence is never
    applied. A hash mismatch dismisses the proposal instead of mutating a file
    that changed since the pass started.
    """
    store = Path(project_dir) / ".magnolia"
    reflex = store / "reflex"
    if artifact is None:
        artifact = json.loads((reflex / PROPOSAL_FILE).read_text())
    config = config or DreamingConfig(**{
        k: v for k, v in (artifact.get("config") or {}).items()
        if k in DreamingConfig.__dataclass_fields__ and k != "tiers"
    })
    checked_at = _now().isoformat()

    results: list[dict[str, Any]] = []
    mutations = 0
    for idx in pending_indices(artifact):
        if mutations >= config.max_apply:
            break
        proposal = artifact["proposals"][idx]
        verdict = proposal["verdict"]
        result = {
            "index": idx,
            "entry": proposal["entry"],
            "verdict": verdict,
            "applied": False,
            "reason": "",
        }
        path = Path(proposal["path"])
        parsed = parse_frontmatter_file(path)
        if parsed is None:
            result["reason"] = "entry file missing"
            artifact["dismissed"].append(idx)
            results.append(result)
            continue
        if _sha256(path) != proposal["sha256"]:
            result["reason"] = "entry changed since proposal (hash mismatch)"
            artifact["dismissed"].append(idx)
            results.append(result)
            continue

        if verdict in ("confirmed", "unverifiable"):
            result["reason"] = "no mutation"
        elif verdict == "stale":
            meta = dict(parsed["meta"] or {})
            meta["dreaming_status"] = "stale"
            meta["dreaming_checked_at"] = checked_at
            atomic_write_text(path, _serialize_entry(meta, parsed["body"]))
            result["applied"] = True
            mutations += 1
        elif verdict == "wrong":
            if not proposal["corrected_text"] or not proposal["evidence"]:
                result["reason"] = "correction missing corrected_text or evidence"
            else:
                meta = dict(parsed["meta"] or {})
                meta["dreaming_status"] = "corrected"
                meta["dreaming_checked_at"] = checked_at
                body = parsed["body"] + _correction_section(proposal, checked_at)
                atomic_write_text(path, _serialize_entry(meta, body))
                result["applied"] = True
                mutations += 1
        else:
            result["reason"] = f"unknown verdict: {verdict}"

        artifact["applied"].append(idx)
        results.append(result)

    atomic_write_text(reflex / PROPOSAL_FILE, json.dumps(artifact, indent=2))

    log_path = reflex / LOG_FILE
    with open(log_path, "a", encoding="utf-8") as f:
        for result in results:
            proposal = artifact["proposals"][result["index"]]
            f.write(json.dumps({
                "ts": checked_at,
                "proposal_id": artifact["id"],
                "index": result["index"],
                "entry": result["entry"],
                "verdict": result["verdict"],
                "applied": result["applied"],
                "reason": result["reason"],
                "evidence_count": len(proposal.get("evidence") or []),
            }) + "\n")

    if mutations and config.commit:
        from . import versioning
        versioning.commit_all(store, f"dreaming: {mutations} correction(s)")

    verdict_counts = {v: 0 for v in VERDICTS}
    for proposal in artifact["proposals"]:
        verdict_counts[proposal["verdict"]] = verdict_counts.get(proposal["verdict"], 0) + 1

    corrections = [r for r in results if r["verdict"] == "wrong" and r["applied"]]
    acceptance = {
        "bounded_scope": (
            len(artifact["proposals"]) <= config.max_entries
            and mutations <= config.max_apply
        ),
        "corrections_have_evidence": all(
            len(artifact["proposals"][r["index"]].get("evidence") or []) > 0
            for r in corrections
        ),
        "hash_guard_on_every_mutation": all(
            r["applied"] for r in results if r["verdict"] in ("stale", "wrong")
            and r["applied"]
        ),
        "no_unverifiable_mutations": all(
            not r["applied"] for r in results if r["verdict"] == "unverifiable"
        ),
    }
    return {
        "checked": len(artifact["proposals"]),
        "verdicts": verdict_counts,
        "applied": mutations,
        "skipped": sum(1 for r in results if not r["applied"]),
        "dismissed": len(artifact["dismissed"]),
        "results": results,
        "acceptance": acceptance,
        "proposal_path": str(reflex / PROPOSAL_FILE),
        "log_path": str(log_path),
        "config": asdict(config),
    }


def run_dreaming(
    project_dir: str | Path,
    config: DreamingConfig | None = None,
    judge: Callable[[dict[str, Any], str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """One full pass: propose, then deterministically apply. Returns a
    JSON-serializable summary for the task-pack run record."""
    config = config or DreamingConfig()
    artifact = propose_dreaming(project_dir, config=config, judge=judge)
    return apply_dreaming(project_dir, artifact=artifact, config=config)


def render_dreaming_report(summary: dict[str, Any]) -> str:
    """Human-readable per-entry report for the task-pack candidate file."""
    lines = [
        "# Dreaming pass report",
        "",
        f"- entries checked: {summary['checked']}",
        f"- mutations applied: {summary['applied']}",
        f"- skipped: {summary['skipped']}",
        f"- dismissed: {summary['dismissed']}",
        f"- verdicts: {json.dumps(summary['verdicts'])}",
        "",
        "| Entry | Verdict | Applied | Reason |",
        "|---|---|---|---|",
    ]
    for result in summary["results"]:
        lines.append(
            f"| {result['entry']} | {result['verdict']} | "
            f"{'yes' if result['applied'] else 'no'} | {result['reason']} |"
        )
    lines += ["", "## Acceptance", ""]
    for key, value in summary["acceptance"].items():
        lines.append(f"- {key}: {'PASS' if value else 'FAIL'}")
    return "\n".join(lines) + "\n"
