"""Job-completion notice queue — the poller → plugin notification bridge.

The poller detects the moment a job reaches a terminal state, but until now
nothing notified the user (they had to ask in chat to learn a job finished).
This module appends one JSONL line per finished job to
``<project>/.magnolia/.job-notices.jsonl``; the ``magnolia-job-notify.ts``
opencode plugin polls that file and relays a toast, then drains it through
this module's command-line entry point.

Mirrors ``.distill-notices`` in spirit, but consumer-driven: the plugin drains
on its own timer because no opencode events fire while the user is idle (and
the poller runs whether a session is active or not).

Every writer and consumer locks ``.job-notices.jsonl.lock`` with ``flock``.
The sidecar is persistent and is never replaced or unlinked, so replacing the
queue cannot move the lock to another inode. OS lock ownership ends on close
or process death; there is no stale lock file to reclaim.
"""
from __future__ import annotations

import fcntl
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.atomic_io import atomic_write_text


def _notices_path(project_dir: str) -> Path:
    return Path(project_dir) / ".magnolia" / ".job-notices.jsonl"


def _lock_path(project_dir: str) -> Path:
    queue = _notices_path(project_dir)
    return queue.with_name(queue.name + ".lock")


class JobNoticeLock:
    """Exclusive sidecar flock shared by every job-notice queue participant."""

    def __init__(self, project_dir: str, wait: float | None = None) -> None:
        self.path = _lock_path(project_dir)
        self.wait = wait
        self.held = False
        self.last_error: str | None = None
        self._handle = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a+", encoding="utf-8")
            if self.wait is None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + self.wait
                while True:
                    try:
                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self._handle.close()
                            self._handle = None
                            return False
                        time.sleep(0.02)
            self.held = True
            return True
        except OSError as error:
            self.last_error = str(error)
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            return False

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if self.held:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.held = False
            self._handle.close()
            self._handle = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


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
        # Acquire before opening the queue: an already-open append descriptor
        # would otherwise follow a replaced queue inode and strand the notice.
        with JobNoticeLock(project_dir) as held:
            if not held:
                return
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
        if not path.parent.exists():
            return []
        with JobNoticeLock(project_dir, wait=1.0) as held:
            if not held or not path.exists():
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


def _claim(project_dir: str, limit: int) -> int:
    """Peek under the shared lock, print the rows, and hold the lock until the
    caller answers on stdin: ``commit`` consumes the peeked lines, anything
    else (``release``, EOF when the caller dies) leaves the queue untouched."""
    import sys

    from compchem_memory.hook.notices import _peek_jobs, _without

    rows, consumed, lock = _peek_jobs(project_dir, limit)
    try:
        print(json.dumps(rows, ensure_ascii=False), flush=True)
        if lock is None:
            return 0
        if sys.stdin.readline().strip() == "commit":
            path = _notices_path(project_dir)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            kept = _without(lines, consumed)
            if kept:
                atomic_write_text(path, "\n".join(kept) + "\n")
            else:
                path.unlink(missing_ok=True)
        return 0
    finally:
        if lock is not None:
            lock.release()


def _main() -> int:
    """Let the TypeScript plugin use this exact locked implementation."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--drain", metavar="PROJECT_DIR")
    mode.add_argument("--claim", metavar="PROJECT_DIR")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.claim:
        return _claim(args.claim, args.limit)
    print(json.dumps(drain_job_notices(args.drain, args.limit), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
