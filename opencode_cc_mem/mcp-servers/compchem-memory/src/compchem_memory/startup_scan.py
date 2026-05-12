"""Startup scan: walk .magnolia/sessions/, distill any session without a .distilled marker."""

import json
from pathlib import Path
from typing import Any

from compchem_memory.extraction import AutomaticMemoryExtractor


def scan_and_distill(project_dir: str) -> dict[str, Any]:
    """Find session JSONL files without a sibling .distilled marker; distill them;
    write the marker. Idempotent — re-running produces no new entries.

    Returns: {"scanned": int, "distilled": int, "skipped": int}.
    """
    pd = Path(project_dir)
    sessions_dir = pd / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    extractor = AutomaticMemoryExtractor(str(pd))
    scanned = distilled = skipped = 0

    for session_path in sorted(sessions_dir.glob("*.jsonl")):
        scanned += 1
        marker = session_path.with_suffix(".distilled")
        if marker.exists():
            skipped += 1
            continue
        try:
            extractor.extract_and_save(session_path, str(pd))
            marker.write_text(
                json.dumps({"distilled_at": _now_iso(), "events_path": session_path.name})
                + "\n"
            )
            distilled += 1
        except Exception as e:
            print(f"[startup_scan] failed on {session_path.name}: {e}")
            continue

    return {"scanned": scanned, "distilled": distilled, "skipped": skipped}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
