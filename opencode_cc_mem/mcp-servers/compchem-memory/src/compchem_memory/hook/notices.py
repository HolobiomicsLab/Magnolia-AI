"""Job completions and unshown critic warnings for a hook response.

Notices are consumed only after delivery: ``peek_notices`` reads without
removing queue lines, and the CLI calls ``commit_notices`` once the adapter
reports that it emitted the ``notice`` field. A notice the client could not
show stays queued for the next event.

The job queue's shared mkdir lock is held from peek through delivery and
commit. This prevents another consumer from displaying the same lines in the
intervening interval; ``release_notices`` unlocks an undelivered response
without consuming it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from compchem_memory.atomic_io import atomic_write_text
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState
from compchem_memory.job_notices import JobNoticeLock, _notices_path


_ICONS = {
    "success": "✅",
    "science_failure": "❌",
    "infra_failure": "⚠️",
}
JOB_LIMIT = 10
CRITIC_LIMIT = JOB_LIMIT
NOTICE_LINE_LIMIT = 160
NOTICE_TEXT_LIMIT = (JOB_LIMIT + CRITIC_LIMIT) * NOTICE_LINE_LIMIT + JOB_LIMIT + CRITIC_LIMIT - 1
_RUN_ID_LIMIT = 64


def _shorten_middle(text: str, limit: int) -> str:
    """Keep both identifying ends of a long display field."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    left = (limit - 1) // 2
    right = limit - left - 1
    return f"{text[:left]}…{text[-right:]}"


@dataclass
class Notices:
    """Notice text to show, and what to consume once it has been delivered."""

    text: str
    job_lines: list[str] = field(default_factory=list)
    critic: list[str] = field(default_factory=list)
    job_lock: JobNoticeLock | None = field(default=None, repr=False)


def _job_line(row: dict) -> str:
    category = str(row.get("category") or "")
    icon = _ICONS.get(category, "🔔")
    tool = _shorten_middle(
        str(row.get("tool") if row.get("tool") is not None else "job"), 40,
    )
    state = _shorten_middle(str(row.get("state") or ""), 24)
    run_id = str(row.get("run_id") or "")
    shown_run_id = _shorten_middle(run_id, _RUN_ID_LIMIT)
    return f"{icon} {tool} finished ({state}) — run {shown_run_id}"


def _peek_jobs(project_dir: str, limit: int) -> tuple[list[dict], list[str], JobNoticeLock | None]:
    """Read up to ``limit`` job notices under the shared queue lock.

    Returns the parsed rows, the raw queue lines a commit removes, and the
    held lock. The caller must commit or release that lock. The raw lines
    include malformed lines met before the limit, matching the legacy drain.
    """
    lock = JobNoticeLock(project_dir, wait=0.2)
    if not lock.acquire():
        return [], [], None
    try:
        try:
            lines = _notices_path(project_dir).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lock.release()
            return [], [], None
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
        if not consumed:
            lock.release()
            return [], [], None
        return rows, consumed, lock
    except Exception:
        lock.release()
        raise


def peek_notices(paths: ProjectPaths, state: SessionState) -> Notices | None:
    """Return up to ten job notices and queued critic warnings.

    Queue contents are not removed until delivery, but their lock remains
    held. The CLI must call ``commit_notices`` or ``release_notices``.
    """
    job_lock: JobNoticeLock | None = None
    try:
        jobs: list[dict] = []
        job_lines: list[str] = []
        if os.environ.get("MAGNOLIA_JOB_NOTIFY") != "0":
            jobs, job_lines, job_lock = _peek_jobs(str(paths.project_dir), JOB_LIMIT)
        lines = [_job_line(row) for row in jobs]
        queued = state.data.get("unshown_notices")
        critic = [str(notice) for notice in queued[:CRITIC_LIMIT]] if isinstance(queued, list) else []
        lines.extend(notice[:NOTICE_LINE_LIMIT] for notice in critic)
        if not lines:
            if job_lock is not None:
                job_lock.release()
            return None
        return Notices("\n".join(lines)[:NOTICE_TEXT_LIMIT], job_lines, critic, job_lock)
    except Exception:  # noqa: BLE001 - notices must never break a hook
        if job_lock is not None:
            job_lock.release()
        return None


def claim_opencode_critic_toast(
    store: str, session_id: str, limit: int = JOB_LIMIT,
) -> Notices | None:
    """Peek one OpenCode session's queued critic warnings for its TUI only.

    Like the critic lines of ``peek_notices``, nothing is removed until
    ``settle_opencode_critic_toast`` reports delivery, and a line queued since
    the peek is kept.
    """
    with SessionState.open(store, "opencode", session_id) as state:
        queued = state.data.get("unshown_notices")
        critic = [str(notice) for notice in queued[:limit]] if isinstance(queued, list) else []
    if not critic:
        return None
    return Notices("\n".join(critic), [], critic)


def settle_opencode_critic_toast(
    store: str, session_id: str, notices: Notices, delivered: bool,
) -> None:
    """Consume a delivered critic toast; an undelivered one stays queued."""
    if not delivered or not notices.critic:
        return
    with SessionState.open(store, "opencode", session_id) as state:
        queued = state.data.get("unshown_notices")
        queued = [str(notice) for notice in queued] if isinstance(queued, list) else []
        state.data["unshown_notices"] = _without(queued, notices.critic)


def _without(items: list[str], taken: list[str]) -> list[str]:
    """Remove one occurrence of each taken item, keeping everything added since."""
    remaining = list(items)
    for item in taken:
        if item in remaining:
            remaining.remove(item)
    return remaining


def release_notices(notices: Notices) -> None:
    """Release an undelivered response without consuming either notice kind."""
    if notices.job_lock is not None:
        notices.job_lock.release()
        notices.job_lock = None


def commit_notices(paths: ProjectPaths, harness: str, session_id: str, notices: Notices) -> None:
    """Consume delivered notices, then release the lock held since peek."""
    if notices.job_lines:
        lock = notices.job_lock
        if lock is None or not lock.held:
            raise RuntimeError("job notices were not locked at peek")
        try:
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
        finally:
            release_notices(notices)
    if notices.critic:
        with SessionState.open(paths.store, harness, session_id) as state:
            queued = state.data.get("unshown_notices")
            queued = [str(notice) for notice in queued] if isinstance(queued, list) else []
            state.data["unshown_notices"] = _without(queued, notices.critic)
