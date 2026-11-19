"""Process execution with timeouts, stop callbacks, and process-group control."""

from __future__ import annotations

import errno
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .executors.contracts import RunOutcome


BARRIER_FAILURE_EXIT = 125
BARRIER_EXEC_FAILURE_EXIT = 126
BARRIER_EXEC_NOT_FOUND_EXIT = 127
_BARRIER_SIZE_BYTES = 8
_BARRIER_STUB = f"""
import errno, os, signal, sys
fd = int(sys.argv[1])
client_path = sys.argv[2]
client_argv0 = sys.argv[3]

def fail():
    try:
        os.close(fd)
    except OSError:
        pass
    os._exit({BARRIER_FAILURE_EXIT})

def read_exact(size):
    body = bytearray()
    while len(body) < size:
        try:
            chunk = os.read(fd, min(size - len(body), 65536))
        except Exception:
            fail()
        if not chunk:
            fail()
        body.extend(chunk)
    return bytes(body)

released = read_exact(1)
if released != b"\\n":
    fail()
payload_size = int.from_bytes(read_exact({_BARRIER_SIZE_BYTES}), "big")
payload = read_exact(payload_size)
try:
    os.close(fd)
except OSError:
    os._exit({BARRIER_FAILURE_EXIT})
try:
    cursor = 0
    if len(payload) < {_BARRIER_SIZE_BYTES}:
        raise ValueError
    pair_count = int.from_bytes(payload[:{_BARRIER_SIZE_BYTES}], "big")
    cursor = {_BARRIER_SIZE_BYTES}
    original_env = {{}}
    for _ in range(pair_count):
        if len(payload) - cursor < {2 * _BARRIER_SIZE_BYTES}:
            raise ValueError
        key_size = int.from_bytes(payload[cursor:cursor + {_BARRIER_SIZE_BYTES}], "big")
        cursor += {_BARRIER_SIZE_BYTES}
        value_size = int.from_bytes(payload[cursor:cursor + {_BARRIER_SIZE_BYTES}], "big")
        cursor += {_BARRIER_SIZE_BYTES}
        end = cursor + key_size + value_size
        if end > len(payload):
            raise ValueError
        key = payload[cursor:cursor + key_size]
        cursor += key_size
        original_env[key] = payload[cursor:end]
        cursor = end
    if cursor != len(payload):
        raise ValueError
except Exception:
    os._exit({BARRIER_FAILURE_EXIT})
for signal_name in ("SIGPIPE", "SIGXFSZ"):
    changed_signal = getattr(signal, signal_name, None)
    if changed_signal is not None:
        signal.signal(changed_signal, signal.SIG_DFL)
try:
    os.execve(client_path, [client_argv0, *sys.argv[4:]], original_env)
except OSError as exc:
    code = {BARRIER_EXEC_NOT_FOUND_EXIT} if exc.errno == errno.ENOENT else {BARRIER_EXEC_FAILURE_EXIT}
    detail = os.strerror(exc.errno or errno.EIO).replace("\\n", " ")
    try:
        os.write(2, ("magnolia governed exec failed: " + detail + "\\n").encode())
    except OSError:
        pass
    os._exit(code)
"""


class ProcessStartError(OSError):
    """Raised when governed work cannot cross its pre-exec barrier."""


class _BarrierInterrupted(Exception):
    def __init__(
        self,
        *,
        timed_out: bool,
        stopped_reason: str | None,
        outcome: RunOutcome,
        returncode: int = -signal.SIGKILL,
    ) -> None:
        self.timed_out = timed_out
        self.stopped_reason = stopped_reason
        self.outcome = outcome
        self.returncode = returncode


@dataclass
class ProcessControl:
    should_stop: Callable[[], str | None] | None = None
    on_start: Callable[[int, int], None] | None = None
    on_heartbeat: Callable[[], None] | None = None
    observe: Callable[[Path, Path], RunOutcome | None] | None = None
    poll_interval: float = 0.2
    hold_before_exec: bool = False


@dataclass
class ProcessOutcome:
    returncode: int
    timed_out: bool
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    stopped_reason: str | None = None
    outcome: RunOutcome | None = None
    client_started: bool = True


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
        if control and (control.hold_before_exec or control.on_start is not None):
            try:
                process = _start_governed_process(
                    argv,
                    cwd,
                    env,
                    stdout,
                    stderr,
                    control,
                    deadline=started + timeout_seconds,
                )
            except _BarrierInterrupted as interrupted:
                return ProcessOutcome(
                    returncode=interrupted.returncode,
                    timed_out=interrupted.timed_out,
                    duration_seconds=time.monotonic() - started,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    stopped_reason=interrupted.stopped_reason,
                    outcome=interrupted.outcome,
                    client_started=False,
                )
        else:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            if control and control.on_start:
                try:
                    control.on_start(process.pid, process.pid)
                except Exception as exc:
                    _terminate_tree(process)
                    process.wait()
                    raise RuntimeError(
                        f"process start callback failed: {exc}"
                    ) from exc
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


def _start_governed_process(
    argv: Sequence[str],
    cwd: str | Path,
    env: Mapping[str, str],
    stdout: object,
    stderr: object,
    control: ProcessControl,
    deadline: float,
) -> subprocess.Popen:
    try:
        client_path = _resolve_executable(argv[0], cwd, env)
        environment_payload = _snapshot_environment(env)
    except OSError as exc:
        raise _process_start_error(exc) from exc
    launch_env = dict(env)
    launch_env.pop("__PYVENV_LAUNCHER__", None)
    launch_env.pop(b"__PYVENV_LAUNCHER__", None)
    read_fd = None
    write_fd = None
    try:
        read_fd, write_fd = os.pipe()
        launch_argv = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _BARRIER_STUB,
            str(read_fd),
            client_path,
            argv[0],
            *argv[1:],
        ]
        process = subprocess.Popen(
            launch_argv,
            cwd=str(cwd),
            env=launch_env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            pass_fds=(read_fd,),
        )
    except BaseException as exc:
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if isinstance(exc, OSError):
            raise _process_start_error(exc) from exc
        raise
    os.close(read_fd)
    try:
        if control.on_start:
            control.on_start(process.pid, process.pid)
        if process.poll() is not None:
            raise ProcessStartError(
                errno.EIO, "governed process exited before barrier release"
            )
        try:
            payload_size = len(environment_payload).to_bytes(
                _BARRIER_SIZE_BYTES, "big"
            )
            _write_all(
                write_fd,
                b"\n" + payload_size + environment_payload,
                deadline=deadline,
                control=control,
            )
        except OSError as exc:
            raise _process_start_error(exc) from exc
        except _BarrierInterrupted:
            raise
    except _BarrierInterrupted as interrupted:
        os.close(write_fd)
        _kill_and_reap(process)
        interrupted.returncode = (
            process.returncode
            if process.returncode is not None
            else -signal.SIGKILL
        )
        raise
    except BaseException as exc:
        os.close(write_fd)
        _kill_and_reap(process)
        if isinstance(exc, Exception) and not isinstance(exc, OSError):
            raise ProcessStartError(
                errno.EIO, f"governed pre-exec callback failed: {exc}"
            ) from exc
        raise
    os.close(write_fd)
    return process


def _resolve_executable(
    executable: str, cwd: str | Path, env: Mapping[str, str]
) -> str:
    if os.path.dirname(executable):
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        if not candidate.exists():
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), executable
            )
        if candidate.is_dir() or not os.access(candidate, os.X_OK):
            raise PermissionError(
                errno.EACCES, os.strerror(errno.EACCES), executable
            )
        return os.path.abspath(candidate)
    search_path = os.pathsep.join(os.get_exec_path(env))
    resolved = shutil.which(executable, path=search_path)
    if resolved is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), executable)
    return os.path.abspath(resolved)


def _snapshot_environment(env: Mapping[str, str]) -> bytes:
    entries = [(os.fsencode(key), os.fsencode(value)) for key, value in env.items()]
    payload = bytearray(len(entries).to_bytes(_BARRIER_SIZE_BYTES, "big"))
    for key, value in entries:
        payload.extend(len(key).to_bytes(_BARRIER_SIZE_BYTES, "big"))
        payload.extend(len(value).to_bytes(_BARRIER_SIZE_BYTES, "big"))
        payload.extend(key)
        payload.extend(value)
    return bytes(payload)


def _write_all(
    fd: int,
    body: bytes,
    *,
    deadline: float,
    control: ProcessControl,
) -> None:
    remaining = memoryview(body)
    os.set_blocking(fd, False)
    while remaining:
        reason = _start_stop_reason(control)
        if reason:
            raise _BarrierInterrupted(
                timed_out=False,
                stopped_reason=reason,
                outcome=RunOutcome("cancelled"),
            )
        now = time.monotonic()
        if now >= deadline:
            raise _BarrierInterrupted(
                timed_out=True,
                stopped_reason=None,
                outcome=RunOutcome("timeout"),
            )
        try:
            written = os.write(fd, remaining)
        except BlockingIOError:
            wait_seconds = min(control.poll_interval, max(deadline - now, 0.0))
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_WRITE)
                selector.select(wait_seconds)
            continue
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "short barrier payload write")
        remaining = remaining[written:]


def _start_stop_reason(control: ProcessControl) -> str | None:
    if control.should_stop is None:
        return None
    try:
        return control.should_stop()
    except Exception as exc:
        return f"stop check failed: {exc}"


def _process_start_error(exc: OSError) -> ProcessStartError:
    return ProcessStartError(exc.errno, exc.strerror, exc.filename)


def _kill_and_reap(process: subprocess.Popen) -> None:
    if process.returncode is None and process.poll() is None:
        try:
            _signal_group(process.pid, signal.SIGKILL)
        except PermissionError:
            if process.poll() is None:
                raise
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _proc_state(pid: int) -> str | None:
    try:
        body = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    parts = body.rsplit(b")", 1)
    if len(parts) != 2:
        return None
    fields = parts[1].split()
    if not fields or len(fields[0]) != 1:
        return None
    try:
        return fields[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def _group_alive(pgid: int) -> bool:
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                body = (entry / "stat").read_bytes()
            except OSError:
                continue
            parts = body.rsplit(b")", 1)
            if len(parts) != 2:
                return True
            fields = parts[1].split()
            if (
                len(fields) >= 3
                and fields[2] == str(pgid).encode("ascii")
                and fields[0] != b"Z"
            ):
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
