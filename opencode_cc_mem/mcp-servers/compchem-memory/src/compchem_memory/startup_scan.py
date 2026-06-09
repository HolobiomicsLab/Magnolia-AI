"""Startup scan: walk .magnolia/sessions/, distill any session without a .distilled marker."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from compchem_memory.extraction import AutomaticMemoryExtractor


def scan_and_distill(project_dir: str) -> dict[str, Any]:
    """Find session JSONL files and distill them.

    Closed sessions (stem != current-session-id): commit, then write a .distilled
    marker. Idempotent — a marked file is skipped on later scans.

    The active session (stem == current-session-id): commit, but do NOT write a
    marker — it is still being appended to. The cursor in extraction-state.yaml
    tracks progress. The marker is only written once the session is no longer
    active (a later scan when it has become a closed session).

    Returns: {"scanned": int, "distilled": int, "skipped": int}.
    """
    pd = Path(project_dir)
    sessions_dir = pd / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Identify the active session (if any)
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

    # Also distill the REAL opencode conversations captured for THIS project.
    # The capture plugin (magnolia-session-capture.ts) writes a project-specific
    # mapping at <project>/.magnolia/opencode-sessions.jsonl keyed by
    # MAGNOLIA_PROJECT_DIR. We ONLY read that — no fallback to a workspace-root
    # mapping (which would leak sessions from other projects into this project's
    # staging). If no mapping exists for this project yet, conversation
    # distillation is simply not active for it.
    ingested = 0
    try:
        from compchem_memory.opencode_ingest import ingest_opencode_sessions
        store = pd / ".magnolia"
        mapping = store / "opencode-sessions.jsonl"
        if mapping.exists():
            ingested = len(ingest_opencode_sessions(str(store), str(mapping)))
    except Exception as e:
        print(f"[startup_scan] opencode ingest failed: {e}")

    return {"scanned": scanned, "distilled": distilled, "skipped": skipped,
            "opencode_ingested": ingested}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
