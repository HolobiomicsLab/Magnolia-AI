"""Job-completion notice queue — the poller → plugin notification bridge.

The poller detects the moment a job reaches a terminal state, but until now
nothing notified the user (they had to ask in chat to learn a job finished).
This module appends one JSONL line per finished job to
``<project>/.magnolia/.job-notices.jsonl``; the ``magnolia-job-notify.ts``
opencode plugin polls that file and relays a toast, then drains it.

Mirrors ``.distill-notices`` in spirit, but consumer-driven: the plugin drains
on its own timer because no opencode events fire while the user is idle (and
the poller runs whether a session is active or not).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.atomic_io import atomic_write_text


def _notices_path(project_dir: str) -> Path:
    return Path(project_dir) / ".magnolia" / ".job-notices.jsonl"


def push_job_notice(
    project_dir: str,
    *,
    run_id: str,
    tool: str,
    state: str,
    category: str,
    job_id: str | None = None,
) -> None:
    """Append one terminal-state notice. Never raises — notification must
    never break the poll sweep (the caller wraps this anyway)."""
    try:
        path = _notices_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "tool": tool,
            "state": state,
            "category": category,
        }
        if job_id:
            entry["job_id"] = str(job_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def drain_job_notices(project_dir: str, limit: int = 20) -> list[dict]:
    """Read up to ``limit`` notices and remove them from the queue; the
    remaining lines (if any) stay for the next drain. Malformed lines are
    dropped rather than stalling the queue. Never raises — returns ``[]`` on
    any error, including the normal absent-file steady state."""
    try:
        path = _notices_path(project_dir)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        taken: list[dict] = []
        kept: list[str] = []
        for line in lines:
            if len(taken) < limit:
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue  # malformed: drop
                if isinstance(parsed, dict):
                    taken.append(parsed)
            else:
                kept.append(line)
        if kept:
            atomic_write_text(path, "\n".join(kept) + "\n")
        else:
            path.unlink(missing_ok=True)
        return taken
    except Exception:
        return []
