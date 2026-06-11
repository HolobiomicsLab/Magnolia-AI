"""Startup/timer distillation sweep.

One distillation path: the opencode dialogue transcript is the source of truth.
The tool-event heuristic extractor runs ONLY as a fallback, when the dialogue
path cannot — no `opencode` binary, no LLM, or no captured session mapping.
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.llm import is_llm_available


def _opencode_available() -> bool:
    return shutil.which("opencode") is not None


def scan_and_distill(project_dir: str) -> dict[str, Any]:
    """Distil the project's sessions into staging.

    Dialogue is primary: if a captured opencode mapping exists and both the
    `opencode` binary and an LLM are available, distil the real conversation
    transcripts (incrementally, cursor-based) and nothing else. Otherwise fall
    back to the tool-event heuristic extractor over `.magnolia/sessions/*.jsonl`.

    Returns a dict with a `mode` of "dialogue" or "tool_event".
    """
    pd = Path(project_dir)
    store = pd / ".magnolia"
    mapping = store / "opencode-sessions.jsonl"

    if mapping.exists() and is_llm_available() and _opencode_available():
        return _distill_dialogue(store)
    return _distill_tool_events(pd)


def _distill_dialogue(store: Path) -> dict[str, Any]:
    from compchem_memory.opencode_ingest import ingest_opencode_sessions

    ingested = len(ingest_opencode_sessions(str(store)))
    return {"mode": "dialogue", "opencode_ingested": ingested,
            "scanned": 0, "distilled": 0, "skipped": 0}


def _distill_tool_events(pd: Path) -> dict[str, Any]:
    """Fallback: distil the SessionManager tool-event logs.

    Closed sessions (stem != current-session-id) are committed then sealed with a
    .distilled marker. The active session is committed but NOT sealed — it is
    still being appended to, and the cursor in extraction-state.yaml tracks
    progress so a later scan picks up its new events.
    """
    sessions_dir = pd / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    active_id = ""
    current_file = pd / ".magnolia" / ".current-session-id"
    if current_file.exists():
        try:
            active_id = current_file.read_text().strip()
        except OSError:
            active_id = ""

    extractor = AutomaticMemoryExtractor(str(pd))
    scanned = distilled = skipped = 0

    for session_path in sorted(sessions_dir.glob("*.jsonl")):
        scanned += 1
        marker = session_path.with_suffix(".distilled")
        if marker.exists():
            skipped += 1
            continue
        is_active = session_path.stem == active_id
        try:
            extractor.commit(session_path, str(pd))
            if not is_active:
                marker.write_text(
                    json.dumps({"distilled_at": _now_iso(), "events_path": session_path.name})
                    + "\n"
                )
            distilled += 1
        except Exception as e:
            print(f"[startup_scan] failed on {session_path.name}: {e}")
            continue

    return {"mode": "tool_event", "scanned": scanned, "distilled": distilled,
            "skipped": skipped, "opencode_ingested": 0}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
