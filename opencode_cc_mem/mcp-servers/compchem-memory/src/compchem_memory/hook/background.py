"""Detached session worker for queued application probes and critic jobs."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState, private_mkdir, safe_component

_ATTEMPT_LIMIT = 3


def _mark(store: Path, harness: str, session_id: str, key: str, status: str) -> None:
    """Persist one turn's worker status."""
    with SessionState.open(store, harness, session_id) as state:
        ledger = state.data.get("background_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        row = ledger.get(key)
        row = dict(row) if isinstance(row, dict) else {}
        ledger[key] = {**row, "status": status}
        state.data["background_turns"] = ledger


def _update_pending_attempt(state: SessionState, job: dict, attempts: int) -> None:
    """Charge an attempt only when the worker claims the recovered row."""
    pending = state.data.get("catchup_pending")
    if not isinstance(pending, list):
        return
    for row in pending:
        if not isinstance(row, dict):
            continue
        if row.get("turn_id") == job.get("turn_id") \
                and row.get("agent_id") == job.get("agent_id"):
            row["attempts"] = attempts
            return


def _job_order(job: dict) -> tuple[int, float]:
    """Sort main turns first and children newest first."""
    return (0 if job.get("agent_id") is None else 1, -float(job.get("queued", 0)))


def _claim_next(store: Path, harness: str, session_id: str) -> dict | None:
    """Claim the next eligible job or mark the session worker idle."""
    with SessionState.open(store, harness, session_id) as state:
        queue = state.data.get("background_queue")
        queue = [row for row in queue if isinstance(row, dict)] if isinstance(queue, list) else []
        ledger = state.data.get("background_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        eligible = []
        for job in queue:
            current = ledger.get(job.get("key"))
            attempts = int(current.get("attempts", 0)) if isinstance(current, dict) else 0
            is_done = isinstance(current, dict) and current.get("status") == "done"
            if attempts < _ATTEMPT_LIMIT and not is_done:
                eligible.append(job)
        if not eligible:
            state.data["background_queue"] = []
            state.data["background_worker"] = {"status": "idle", "started": time.time()}
            return None
        job = min(eligible, key=_job_order)
        queue.remove(job)
        state.data["background_queue"] = queue
        current = ledger.get(job["key"])
        current = current if isinstance(current, dict) else {}
        attempts = int(current.get("attempts", 0)) + 1
        ledger[job["key"]] = {
            **current, "status": "running", "attempts": attempts, "started": time.time(),
        }
        state.data["background_turns"] = ledger
        _update_pending_attempt(state, job, attempts)
        return job


def _run_turn(paths: ProjectPaths, payload: dict, job: dict) -> None:
    """Run one queued turn under the existing per-turn lock."""
    store = paths.store
    harness = str(payload["harness"])
    session_id = str(payload["session_id"])
    key = str(job["key"])
    lock_path = store / "hook-workers" / f"{safe_component(key)}.lock"
    private_mkdir(lock_path.parent)
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _mark(store, harness, session_id, key, "failed")
            return
        try:
            from compchem_memory.hook.action_retrieval import probe_application
            from compchem_memory.hook.critic import run_critic

            probe_application(
                paths, harness, session_id, str(job.get("reply_text") or ""), job.get("turn_id"),
            )
            run_critic(paths, harness, session_id, job.get("agent_id"), job.get("turn_id"))
            _mark(store, harness, session_id, key, "done")
        except Exception as error:  # noqa: BLE001 - worker failure is durable evidence
            from compchem_memory.hook.errors import log_error

            _mark(store, harness, session_id, key, "failed")
            log_error(error, harness, "background", store, {}, "catch-up-worker")


def _worker_lock(store: Path, harness: str, session_id: str) -> Path:
    """Return the one lock path shared by every worker for a session."""
    name = f"{safe_component(harness)}--{safe_component(session_id)}--worker.lock"
    return store / "hook-workers" / name


def run_job(job_path: Path) -> None:
    """Drain one session's queued jobs sequentially, then mark the worker idle."""
    job_path = Path(job_path)
    try:
        try:
            payload = json.loads(job_path.read_text(encoding="utf-8"))
            store = Path(payload["store"])
            harness = str(payload["harness"])
            session_id = str(payload["session_id"])
            paths = ProjectPaths(
                Path(payload["root"]), Path(payload["project_dir"]), store,
                str(payload["name"]), bool(payload["root_mode"]),
            )
        except (KeyError, OSError, ValueError, TypeError):
            return
        lock_path = _worker_lock(store, harness, session_id)
        private_mkdir(lock_path.parent)
        with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while (job := _claim_next(store, harness, session_id)) is not None:
                _run_turn(paths, payload, job)
    finally:
        job_path.unlink(missing_ok=True)


def main() -> None:
    """Run the job path supplied by the spawning hook."""
    if len(sys.argv) == 2:
        run_job(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
