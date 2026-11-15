"""Process execution with timeouts, stop callbacks, and process-group control."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .executors.contracts import RunOutcome


@dataclass
class ProcessControl:
    should_stop: Callable[[], str | None] | None = None
    on_start: Callable[[int, int], None] | None = None
    on_heartbeat: Callable[[], None] | None = None
    observe: Callable[[Path, Path], RunOutcome | None] | None = None
    poll_interval: float = 0.2


@dataclass
class ProcessOutcome:
    returncode: int
    timed_out: bool
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    stopped_reason: str | None = None
    outcome: RunOutcome | None = None


def run_process(
    argv: Sequence[str],
    cwd: str | Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    stdout_path: Path,
    stderr_path: Path,
    control: ProcessControl | None = None,
) -> ProcessOutcome:
    if not argv:
        raise ValueError("argv must not be empty")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    stopped_reason: str | None = None
    typed_outcome: RunOutcome | None = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=dict(env),
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError:
            raise
        if control and control.on_start:
            try:
                control.on_start(process.pid, process.pid)
            except Exception as exc:
                _terminate_tree(process)
                process.wait()
                raise RuntimeError(f"process start callback failed: {exc}") from exc
        while True:
            returncode = process.poll()
            if control:
                reason, observed = _check_control(control, stdout_path, stderr_path)
                if reason:
                    stopped_reason = reason
                    typed_outcome = observed or RunOutcome("cancelled")
                    if returncode is None:
                        _terminate_tree(process)
                        returncode = process.wait()
                    break
            if returncode is not None:
                break
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                timed_out = True
                typed_outcome = RunOutcome("timeout")
                _terminate_tree(process)
                returncode = process.wait()
                break
            time.sleep(control.poll_interval if control else 0.2)
    if typed_outcome is None:
        typed_outcome = RunOutcome("success" if returncode == 0 else "failure")
    return ProcessOutcome(
        returncode=returncode,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stopped_reason=stopped_reason,
        outcome=typed_outcome,
    )


def _check_control(
    control: ProcessControl, stdout_path: Path, stderr_path: Path
) -> tuple[str | None, RunOutcome | None]:
    if control.observe:
        try:
            outcome = control.observe(stdout_path, stderr_path)
        except Exception as exc:
            return f"observation failed: {exc}", RunOutcome("cancelled")
        if outcome is not None:
            reason = outcome.limit.reason if outcome.limit else outcome.kind
            return reason, outcome
    if control.should_stop:
        try:
            reason = control.should_stop()
        except Exception as exc:
            return f"stop check failed: {exc}", RunOutcome("cancelled")
        if reason:
            return reason, RunOutcome("cancelled")
    if control.on_heartbeat:
        try:
            control.on_heartbeat()
        except Exception as exc:
            return f"heartbeat failed: {exc}", RunOutcome("cancelled")
    return None, None


def _terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    _signal_group(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_group(process.pid, signal.SIGKILL)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _proc_state(pid: int) -> str | None:
    try:
        body = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = body.rsplit(")", 1)[-1].split()
    return fields[0] if fields else None


def _group_alive(pgid: int) -> bool:
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                body = (entry / "stat").read_text(encoding="utf-8")
            except OSError:
                continue
            fields = body.rsplit(")", 1)[-1].split()
            if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] != "Z":
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_alive(pid: int) -> bool:
    state = _proc_state(pid)
    if state is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return state != "Z"


def stop_process_group(pgid: int, grace_seconds: float = 5.0) -> None:
    if pgid <= 0:
        raise ValueError(f"invalid process group id: {pgid}")
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.1)
    _signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.1)
    raise RuntimeError(f"process group {pgid} survived SIGKILL")


def stop_process(pid: int, grace_seconds: float = 5.0) -> None:
    if pid <= 0:
        raise ValueError(f"invalid process id: {pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.1)
    raise RuntimeError(f"process {pid} survived SIGKILL")
