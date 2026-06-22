"""Rolling LLM handover for boot-context.md.

Replaces the raw-events "session context" block with an LLM-written handover
(Done / In progress / To do / Key files) merged from the project's last opencode
session transcript. State persists in `.magnolia/.handover-state.md` (machine-
owned, never hand-edited); `assemble_context` inlines a rendered view of it.

Defensive by design: generate_handover returns None on any no-op/failure logic
path; the boot worker additionally wraps each step in try/except, so even a hard
filesystem error in the final write cannot crash the launch.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from compchem_memory.atomic_io import atomic_write_text

HANDOVER_STATE_FILE = ".handover-state.md"
HANDOVER_CURSOR_FILE = ".handover-cursor.json"
TOMBSTONE_HEADING = "## Won't-do / Archived"

HANDOVER_MERGE_PROMPT = """You maintain a ROLLING HANDOVER for a computational-chemistry
agent project, so the next session knows exactly where the work stands. You are given the
CURRENT HANDOVER and the NEW SESSION TRANSCRIPT (user/assistant text + reasoning since the
handover was last updated). Rewrite the handover by MERGING the new session into it.

Output ONLY the handover as markdown, with these sections (omit a section only if it would
be empty, except keep '## Won't-do / Archived' verbatim if present):
- '## Done' — completed work, with the specific result (residues / scores / run dirs / files).
- '## In progress' — started but not finished, with current state.
- '## To do' — pending next steps.
- '## Stale?' — items carried with no activity (see rules); flag for a keep/kill decision.
- '## Key files' — paths worth knowing, one per line with a short note.
- '## Won't-do / Archived' — tombstones; see rules.

MERGE RULES:
- Carry every existing item forward UNCHANGED unless the transcript gives evidence to move it.
- Move an item to '## Done' when the transcript shows it completed.
- DROP an item only on EXPLICIT evidence it was abandoned or superseded; otherwise KEEP it.
  Default is keep, never silently delete.
- If an item has been carried across sessions with no activity, move it to '## Stale?' with a
  short "(no activity)" note — surface it, do not delete it.
- NEVER re-add anything listed under '## Won't-do / Archived'. Preserve that section as-is.
- Ground items in specifics (residues, scores, IDs, run directories, file paths), not vague summaries.

IGNORE memory-system plumbing: the agent's use of memory_search, memory_get_context,
consolidation / merge proposals, distillation, promotion, staging, scan_headers, and pending-
proposal notices are NOT work to report. Capture the SCIENCE and the user's decisions.
"""


def render_for_boot_context(state_text: str) -> str:
    """Return the handover view for boot-context: strip the tombstone section,
    keep everything else (including '## Stale?')."""
    out: list[str] = []
    skipping = False
    for line in state_text.splitlines():
        if line.strip() == TOMBSTONE_HEADING:
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).strip()


def read_handover_block(store: Path) -> str | None:
    """Read `.handover-state.md` and return its rendered boot-context view, or
    None when there is no (non-empty) handover yet."""
    p = Path(store) / HANDOVER_STATE_FILE
    if not p.exists():
        return None
    try:
        text = p.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    rendered = render_for_boot_context(text)
    return rendered or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_cursor(store: Path) -> dict | None:
    try:
        return json.loads((Path(store) / HANDOVER_CURSOR_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cursor(store: Path, sid: str, cursor: str | None) -> None:
    atomic_write_text(
        Path(store) / HANDOVER_CURSOR_FILE,
        json.dumps({"sid": sid, "cursor": cursor, "updated": _now_iso()}) + "\n",
    )


def generate_handover(
    project_dir: str,
    *,
    exporter: Optional[Callable[[str], Optional[dict]]] = None,
    llm: Optional[Callable[..., Optional[str]]] = None,
) -> str | None:
    """Merge the project's latest-session transcript into the rolling handover.

    Reuses the distillation transcript pipeline (export -> reconstruct -> scrub)
    and the project's per-project session mapping. Returns the state-file path on
    a write, or None on any no-op/failure (no mapping, export failed, nothing new,
    empty transcript, LLM unavailable/failed). Does not raise on any logic path;
    the only residual raise is a hard filesystem failure in the final atomic
    write, which the boot worker contains by wrapping each step in try/except.
    """
    from compchem_memory.opencode_ingest import (
        export_session, reconstruct_transcript, scrub_secrets,
        _latest_sid, _messages_after_cursor, _last_message_id,
    )
    from compchem_memory.llm import call_llm

    exporter = exporter or export_session
    llm = llm or call_llm

    store = Path(project_dir) / ".magnolia"
    mapping = store / "opencode-sessions.jsonl"
    if not mapping.exists():
        return None
    sid = _latest_sid(mapping)
    if not sid:
        return None

    cur = _read_cursor(store)
    cursor = cur.get("cursor") if (cur and cur.get("sid") == sid) else None

    export = exporter(sid)
    if not export:
        return None
    new_msgs = _messages_after_cursor(export.get("messages") or [], cursor)
    if not new_msgs:
        return None
    transcript = scrub_secrets(reconstruct_transcript({"messages": new_msgs}))
    if not transcript.strip():
        return None

    base = ""
    state_path = store / HANDOVER_STATE_FILE
    if state_path.exists():
        try:
            base = state_path.read_text()
        except OSError:
            base = ""

    user_content = (
        "=== CURRENT HANDOVER (base to update) ===\n"
        + (base.strip() or "(none yet — create the first handover)")
        + "\n\n=== NEW SESSION TRANSCRIPT (since last handover) ===\n"
        + transcript
    )
    merged = llm(HANDOVER_MERGE_PROMPT, user_content, max_tokens=3000)
    if not merged or not merged.strip():
        return None

    atomic_write_text(state_path, merged.strip() + "\n")
    _write_cursor(store, sid, _last_message_id(new_msgs) or cursor)
    return str(state_path)
