"""Detached application-probe and critic worker for hook-limited clients."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState, private_mkdir


def _mark(store: Path, harness: str, session_id: str, key: str, status: str) -> None:
    with SessionState.open(store, harness, session_id) as state:
        ledger = state.data.get("background_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        row = ledger.get(key)
        row = dict(row) if isinstance(row, dict) else {}
        ledger[key] = {**row, "status": status}
        state.data["background_turns"] = ledger


def run_job(job_path: Path) -> None:
    """Run one persisted job under its per-turn lock and mark completion last."""
    try:
        payload = json.loads(Path(job_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    store = Path(payload["store"])
    harness = str(payload["harness"])
    session_id = str(payload["session_id"])
    key = str(payload["key"])
    lock_path = store / "hook-workers" / f"{key}.lock"
    private_mkdir(lock_path.parent)
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            paths = ProjectPaths(
                Path(payload["root"]), Path(payload["project_dir"]), store,
                str(payload["name"]), bool(payload["root_mode"]),
            )
            from compchem_memory.hook.action_retrieval import probe_application
            from compchem_memory.hook.critic import run_critic

            probe_application(
                paths, harness, session_id, str(payload.get("reply_text") or ""),
                payload.get("turn_id"),
            )
            run_critic(
                paths, harness, session_id, payload.get("agent_id"), payload.get("turn_id"),
            )
            _mark(store, harness, session_id, key, "done")
        except Exception as error:  # noqa: BLE001 - worker failure is durable evidence
            from compchem_memory.hook.errors import log_error

            _mark(store, harness, session_id, key, "failed")
            log_error(error, harness, "background", store, {}, "catch-up-worker")
        finally:
            Path(job_path).unlink(missing_ok=True)


def main() -> None:
    """Run the job path supplied by the spawning hook."""
    if len(sys.argv) == 2:
        run_job(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
