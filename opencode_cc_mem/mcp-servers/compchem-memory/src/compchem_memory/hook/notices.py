"""Drain job completions and unshown critic warnings for a hook response."""

from __future__ import annotations

import os

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState
from compchem_memory.job_notices import drain_job_notices


_ICONS = {
    "success": "✅",
    "science_failure": "❌",
    "infra_failure": "⚠️",
}


def _job_line(row: dict) -> str:
    category = str(row.get("category") or "")
    icon = _ICONS.get(category, "🔔")
    tool = str(row.get("tool") if row.get("tool") is not None else "job")
    state = str(row.get("state") or "")
    run_id = str(row.get("run_id") or "")
    return f"{icon} {tool} finished ({state}) — run …{run_id[-24:]}"[:160]


def _critic_lines(state: SessionState) -> list[str]:
    notices = state.data.get("unshown_notices")
    state.data["unshown_notices"] = []
    if not isinstance(notices, list):
        return []
    return [str(notice) for notice in notices]


def pending_notices(paths: ProjectPaths, state: SessionState) -> str | None:
    """Drain up to ten job notices, then consume queued critic warnings."""
    try:
        jobs = []
        if os.environ.get("MAGNOLIA_JOB_NOTIFY") != "0":
            jobs = drain_job_notices(str(paths.project_dir), limit=10)
        lines = [_job_line(row) for row in jobs if isinstance(row, dict)]
        lines.extend(_critic_lines(state))
        return "\n".join(lines) or None
    except Exception:  # noqa: BLE001 - notices must never break a hook
        return None
