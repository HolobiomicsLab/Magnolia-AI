"""Job completions and unshown critic warnings for a hook response.

Notices are consumed only after delivery: ``peek_notices`` reads without side
effects, and the CLI calls ``commit_notices`` once the adapter reports that it
emitted the ``notice`` field. A notice the client could not show stays queued
for the next event.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from compchem_memory.atomic_io import atomic_write_text
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState
from compchem_memory.job_notices import _notices_path


_ICONS = {
    "success": "✅",
    "science_failure": "❌",
    "infra_failure": "⚠️",
}
JOB_LIMIT = 10


@dataclass
class Notices:
    """Notice text to show, and what to consume once it has been delivered."""

    text: str
    job_lines: list[str] = field(default_factory=list)
    critic: list[str] = field(default_factory=list)


def _job_line(row: dict) -> str:
    category = str(row.get("category") or "")
    icon = _ICONS.get(category, "🔔")
    tool = str(row.get("tool") if row.get("tool") is not None else "job")
    state = str(row.get("state") or "")
    run_id = str(row.get("run_id") or "")
    return f"{icon} {tool} finished ({state}) — run …{run_id[-24:]}"[:160]


def _peek_jobs(project_dir: str, limit: int) -> tuple[list[dict], list[str]]:
    """Read up to ``limit`` job notices without removing them.

    Returns the parsed rows and the raw queue lines a commit removes: the rows
    shown and the malformed lines met before the limit, which the legacy drain
    (``job_notices.drain_job_notices``) also drops.
    """
    try:
        lines = _notices_path(project_dir).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], []
    rows: list[dict] = []
    consumed: list[str] = []
    for line in lines:
        if len(rows) >= limit:
            break
        consumed.append(line)
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows, consumed


def peek_notices(paths: ProjectPaths, state: SessionState) -> Notices | None:
    """Return up to ten job notices and the queued critic warnings; consume nothing."""
    try:
        jobs: list[dict] = []
        job_lines: list[str] = []
        if os.environ.get("MAGNOLIA_JOB_NOTIFY") != "0":
            jobs, job_lines = _peek_jobs(str(paths.project_dir), JOB_LIMIT)
        lines = [_job_line(row) for row in jobs]
        queued = state.data.get("unshown_notices")
        critic = [str(notice) for notice in queued] if isinstance(queued, list) else []
        lines.extend(critic)
        if not lines:
            return None
        return Notices("\n".join(lines), job_lines, critic)
    except Exception:  # noqa: BLE001 - notices must never break a hook
        return None


def _without(items: list[str], taken: list[str]) -> list[str]:
    """Remove one occurrence of each taken item, keeping everything added since."""
    remaining = list(items)
    for item in taken:
        if item in remaining:
            remaining.remove(item)
    return remaining


def commit_notices(paths: ProjectPaths, harness: str, session_id: str, notices: Notices) -> None:
    """Consume delivered notices: their job-queue lines and their critic warnings."""
    if notices.job_lines:
        path = _notices_path(str(paths.project_dir))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        kept = _without(lines, notices.job_lines)
        if kept:
            atomic_write_text(path, "\n".join(kept) + "\n")
        else:
            path.unlink(missing_ok=True)
    if notices.critic:
        with SessionState.open(paths.store, harness, session_id) as state:
            queued = state.data.get("unshown_notices")
            queued = [str(notice) for notice in queued] if isinstance(queued, list) else []
            state.data["unshown_notices"] = _without(queued, notices.critic)
