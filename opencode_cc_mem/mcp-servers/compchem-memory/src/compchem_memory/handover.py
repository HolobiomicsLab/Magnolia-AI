"""Rolling LLM handover for boot-context.md.

Replaces the raw-events "session context" block with an LLM-written handover
(Done / In progress / To do / Key files) merged from the project's last opencode
session transcript. State persists in `.magnolia/.handover-state.md` (machine-
owned, never hand-edited); `assemble_context` inlines a rendered view of it.

Defensive by design: generate_handover returns None on any failure and never
raises, so it is safe as a boot-worker step.
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
