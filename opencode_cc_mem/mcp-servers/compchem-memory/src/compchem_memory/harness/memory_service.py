"""Project-bound lifecycle for the loopback compchem-memory HTTP service."""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from compchem_memory.hook.runner import repository_runner_path
from compchem_memory.process_identity import (
    verified_process_start_token as _verified_process_start_token,
)

try:
    from campaign_runner.executors.registry import ExecutorRefused
except ImportError:  # Standalone memory-server installation.
    class ExecutorRefused(RuntimeError):
        """Raised when a safe memory-service launch cannot be prepared."""

TOKEN_ENV = "MAGNOLIA_MEMORY_SERVICE_TOKEN"
DEFAULT_READY_TIMEOUT_SECONDS = 10.0
READY_INTERVAL_SECONDS = 0.1
HEALTH_TIMEOUT_SECONDS = 2.0
SERVICE_MODULE = "compchem_memory.memory_http"
STARTUP_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
_UNMASK_AND_EXEC = (
    "import os, signal, sys; "
    "signal.pthread_sigmask(signal.SIG_UNBLOCK, "
    "{signal.SIGINT, signal.SIGTERM, signal.SIGHUP}); "
    "os.execvp(sys.argv[1], sys.argv[1:])"
)
LOGGER = logging.getLogger(__name__)
SAFE_BASE_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "ANTHROPIC_BASE_URL",
        "DEEPSEEK_BASE_URL",
        "OPENAI_BASE_URL",
        "KIMI_BASE_URL",
        "OPENROUTER_BASE_URL",
        "MISTRAL_BASE_URL",
        "ZAI_BASE_URL",
        "OPENROUTER_REFERER",
        "OPENROUTER_TITLE",
        "OPENCODE_DB",
    }
)
SERVER_RUNTIME_ENV = frozenset(
    {
        "MAGNOLIA_ADMISSION_GATE",
        "MAGNOLIA_AUTO_CONFIRM",
        "MAGNOLIA_CONSOLIDATION_AUTO",
        "MAGNOLIA_CONSOLIDATION_AUTO_MAX",
        "MAGNOLIA_CONSOLIDATION_FLOOR",
        "MAGNOLIA_CONSOLIDATION_SEED",
        "MAGNOLIA_CONSOLIDATION_SIBLING",
        "MAGNOLIA_CRITIC",
        "MAGNOLIA_CRITIC_BASE_URL",
        "MAGNOLIA_CRITIC_MODEL",
        "MAGNOLIA_CRITIC_PROVIDER",
        "MAGNOLIA_DISABLE_INLINE_EXTRACT",
        "MAGNOLIA_DISTILL_INTERVAL_MIN",
        "MAGNOLIA_DISTILL_SECOND_PASS",
        "MAGNOLIA_DREAMING_JUDGE",
        "MAGNOLIA_HANDOVER_SKIP_EXPORT",
        "MAGNOLIA_LLM_MODEL",
        "MAGNOLIA_LLM_PROVIDER",
        "MAGNOLIA_MEMORY_SERVICE_GRACE_S",
        "MAGNOLIA_MEMORY_SERVICE_WATCHDOG_S",
        "MAGNOLIA_OFFPEAK_DISABLE",
        "MAGNOLIA_OPENCODE_DIR",
        "MAGNOLIA_OPENCODE_V2",
        "MAGNOLIA_OPENCODE_V2_BIN",
        "MAGNOLIA_OPENROUTER_PROVIDER",
        "MAGNOLIA_OPENROUTER_STICKY",
        "MAGNOLIA_PROSE_NUMBERS",
        "MAGNOLIA_RETIREMENT",
        "MAGNOLIA_RETIREMENT_MIN_OPPORTUNITIES",
        "MAGNOLIA_RETIREMENT_PROPOSAL_GRACE_DAYS",
        "MAGNOLIA_RULES_DIR",
        "MAGNOLIA_SESSION_IDLE_CLOSE_HOURS",
        "MAGNOLIA_SMOKE_INTERVAL_H",
        "MAGNOLIA_TRANSCRIPT_TOOL_CHARS",
    }
)
PROVIDER_KEYS = {
    "anthropic": ("MAGNOLIA_LLM_API_KEY", "ANTHROPIC_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("MAGNOLIA_OPENAI_API_KEY", "OPENAI_API_KEY"),
    "kimi": ("KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "mistral": ("MAGNOLIA_MISTRAL_API_KEY", "MISTRAL_API_KEY"),
    "zai": ("ZHIPU_API_KEY", "ZAI_API_KEY"),
}


@dataclass(frozen=True)
class ServiceHandle:
    """One reference to a verified per-project memory service."""

    url: str
    project_digest: str
    process_identity: str
    project_dir: Path
    holder_id: str
    service_identity: str
    token_file: Path
    authorization_env: str = TOKEN_ENV

    def route(self) -> dict[str, str]:
        """Return the route plus private token-file pointer for launch preparation."""
        return {
            "transport": "http",
            "url": self.url,
            "authorization_env": self.authorization_env,
            "token_file": str(self.token_file),
        }


def encode_handle(handle: ServiceHandle) -> str:
    """Encode a secret-free handle for release by the parent launcher."""
    payload = {
        "url": handle.url,
        "project_digest": handle.project_digest,
        "process_identity": handle.process_identity,
        "project_dir": str(handle.project_dir),
        "holder_id": handle.holder_id,
        "service_identity": handle.service_identity,
        "token_file": str(handle.token_file),
        "authorization_env": handle.authorization_env,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii")


def decode_handle(value: str) -> ServiceHandle:
    """Decode a launcher-owned service handle without reading any credential."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        return ServiceHandle(
            url=str(payload["url"]),
            project_digest=str(payload["project_digest"]),
            process_identity=str(payload["process_identity"]),
            project_dir=Path(payload["project_dir"]).resolve(),
            holder_id=str(payload["holder_id"]),
            service_identity=str(payload["service_identity"]),
            token_file=Path(payload["token_file"]).resolve(),
            authorization_env=str(payload.get("authorization_env", TOKEN_ENV)),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExecutorRefused("invalid memory service release handle") from exc


def project_digest(project_dir: Path) -> str:
    """Return the stable digest of a canonical project path."""
    canonical = str(project_dir.expanduser().resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def private_state_dir(project_dir: Path) -> Path:
    """Return the non-work-tree state directory for this project's service."""
    digest = project_digest(project_dir)
    return (Path.home() / ".magnolia" / "services" / digest).resolve()


def ensure_memory_service(context: Any) -> ServiceHandle:
    """Acquire a ready, authenticated service bound to ``context.project_dir``."""
    holder = _new_holder(_holder_pid(context))
    project = _context_project(context)
    environment = _server_environment(context)
    _require_provider_key(context, environment)
    state = private_state_dir(project)
    _ensure_private_state_dir(state)
    with _state_lock(state):
        record = _read_record(state)
        if record is not None:
            handle = _reuse(record, state, project, holder)
            if handle is not None:
                return handle
            _discard_record(record, state, project_digest(project))
        return _start_service(context, project, state, environment, holder)


def release_memory_service(handle: ServiceHandle) -> bool:
    """Release one holder and stop only its verified service when references reach zero."""
    if handle.project_digest != project_digest(handle.project_dir):
        return False
    state = private_state_dir(handle.project_dir)
    with _state_lock(state):
        record = _read_record(state)
        if not _handle_matches(handle, record):
            return False
        holders = [
            holder
            for holder in _active_holders(record.get("holders", []))
            if holder.get("id") != handle.holder_id
        ]
        if holders:
            record["holders"] = holders
            _write_record(state, record)
            return False
        stopped = False
        if _service_signal_safe(record):
            stopped = _terminate_owned(record)
        if stopped:
            _remove_state_files(state)
        return stopped


def token_for_handle(handle: ServiceHandle) -> str:
    """Read the service bearer token for delivery through the client environment."""
    try:
        mode = handle.token_file.stat().st_mode & 0o777
        token = handle.token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ExecutorRefused("memory service bearer token is unavailable") from exc
    if mode != 0o600 or not token:
        raise ExecutorRefused("memory service bearer token file is not private and usable")
    return token


def _context_project(context: Any) -> Path:
    value = getattr(context, "project_dir", None)
    if value is None:
        raise ExecutorRefused("memory service requires a canonical project directory")
    project = Path(value).expanduser().resolve()
    if not project.is_dir():
        raise ExecutorRefused(f"memory service project does not exist: {project}")
    return project


def _server_environment(context: Any) -> dict[str, str]:
    """Build the service's explicit environment allowlist.

    The client environment can contain subscription credentials and keys for
    unrelated providers.  Only process/runtime paths, the selected memory
    provider's API key, and the memory provider/model selectors cross the
    service boundary.
    """
    supplied = getattr(context, "environment", None)
    base = os.environ if supplied is None else supplied
    setup = getattr(context, "setup", {}) or {}
    provider = str(
        setup.get("memory_provider") or base.get("MAGNOLIA_MEMORY_PROVIDER") or ""
    ).strip().lower()
    allowed = set(SAFE_BASE_ENV | SERVER_RUNTIME_ENV)
    allowed.update(PROVIDER_KEYS.get(provider, ()))
    environment = {
        str(key): str(value)
        for key, value in base.items()
        if key in allowed and str(value)
    }
    if provider:
        environment["MAGNOLIA_MEMORY_PROVIDER"] = provider
    model = setup.get("memory_model") or base.get("MAGNOLIA_MEMORY_MODEL")
    if model:
        environment["MAGNOLIA_MEMORY_MODEL"] = str(model)
    return environment


def _require_provider_key(context: Any, environment: Mapping[str, str]) -> None:
    setup = getattr(context, "setup", {}) or {}
    provider = str(
        setup.get("memory_provider") or environment.get("MAGNOLIA_MEMORY_PROVIDER") or ""
    ).strip().lower()
    if not provider:
        raise ExecutorRefused(
            "memory service has no configured memory_provider; configure Magnolia "
            "memory with an API-billed provider before launch"
        )
    names = PROVIDER_KEYS.get(provider)
    if names is None:
        raise ExecutorRefused(
            f"memory service provider '{provider}' is unsupported; configure a supported "
            "memory_provider before launch"
        )
    if any(environment.get(name, "").strip() for name in names):
        return
    suffix = f" for provider '{provider}'" if provider else ""
    raise ExecutorRefused(
        "memory service requires a provider API key"
        f"{suffix} in the server environment; configure Magnolia memory before launch"
    )


@contextmanager
def _state_lock(state: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold the private service lock, optionally refusing contention immediately."""
    _ensure_private_state_dir(state)
    path = state / "service.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        os.chmod(path, 0o600)
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_record(state: Path) -> dict[str, Any] | None:
    path = state / "service.json"
    try:
        if not _private_owned(state, 0o700):
            return None
        if not _private_owned(path, 0o600):
            if path.exists():
                LOGGER.warning(
                    "memory service record is not private; leaving any named process untouched"
                )
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        LOGGER.warning(
            "memory service record is unreadable; leaving any named process untouched"
        )
        return None
    if not isinstance(value, dict):
        LOGGER.warning(
            "memory service record is not an object; leaving any named process untouched"
        )
        return None
    return value


def _write_record(state: Path, record: Mapping[str, Any]) -> None:
    path = state / "service.json"
    temporary = state / f".{path.name}.{os.getpid()}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(state, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _reuse(
    record: dict[str, Any],
    state: Path,
    project: Path,
    holder: dict[str, Any],
) -> ServiceHandle | None:
    expected_digest = project_digest(project)
    if record.get("provisional") is True:
        return None
    try:
        pid = int(record["pid"])
        port = int(record["port"])
        identity = str(record["process_identity"])
        service_identity = str(record["service_identity"])
        token_file = Path(str(record["token_file"])).resolve()
    except (KeyError, TypeError, ValueError, OSError):
        return None
    if record.get("project_digest") != expected_digest:
        return None
    if token_file != (state / "bearer-token").resolve():
        return None
    if not _service_process_matches(record):
        return None
    try:
        if token_file.stat().st_mode & 0o777 != 0o600:
            return None
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    url = f"http://127.0.0.1:{port}/mcp"
    active_holders = _active_holders(record.get("holders", []))
    if not active_holders:
        return None
    if _process_is_stopped(pid):
        return None
    health = None
    for attempt in range(2):
        health = _health(url, token, HEALTH_TIMEOUT_SECONDS)
        if health is not None:
            break
        if attempt == 0:
            time.sleep(READY_INTERVAL_SECONDS)
    if health != {
        "project_digest": expected_digest,
        "process_identity": identity,
        "service_identity": service_identity,
    }:
        return None
    if not _listener_owned_by_pid(pid, port):
        return None
    if not _holder_is_live(holder):
        raise ExecutorRefused("memory service holder identity is no longer live")
    record["holders"] = [*active_holders, holder]
    _write_record(state, record)
    return _handle(record, project, holder["id"])


def _start_service(
    context: Any,
    project: Path,
    state: Path,
    environment: dict[str, str],
    holder: dict[str, Any],
) -> ServiceHandle:
    setup = getattr(context, "setup", {}) or {}
    acquirer_pid = os.getpid()
    acquirer_identity = _process_identity(acquirer_pid)
    if not acquirer_identity:
        raise ExecutorRefused("memory service acquirer identity could not be verified")
    requested_port = setup.get("memory_service_port")
    port = _choose_port(requested_port)
    token = secrets.token_urlsafe(32)
    service_identity = uuid.uuid4().hex
    token_file = state / "bearer-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    log_path = state / "service.log"
    log_path.touch(mode=0o600, exist_ok=True)
    os.chmod(log_path, 0o600)
    python = str(
        getattr(context, "python", None)
        or getattr(context, "python_executable", None)
        or "python"
    )
    child_environment = dict(environment)
    child_environment.update(
        {
            "HOME": str(Path.home()),
            "MAGNOLIA_PROJECT_DIR": str(project),
            "MAGNOLIA_PROJECT_BOUND": "1",
            "MAGNOLIA_MEMORY_SERVICE_PORT": str(port),
            "MAGNOLIA_MEMORY_SERVICE_IDENTITY": service_identity,
            "MAGNOLIA_MEMORY_SERVICE_ACQUIRER_PID": str(acquirer_pid),
            "MAGNOLIA_MEMORY_SERVICE_ACQUIRER_IDENTITY": acquirer_identity,
            "MAGNOLIA_SERVER_STARTED_AT": str(time.time()),
            TOKEN_ENV: token,
        }
    )
    command = [
        python,
        "-I",
        repository_runner_path(),
        "--memory-http",
        "--project-dir",
        str(project),
        "--state-dir",
        str(state),
    ]
    process = None
    with _startup_signal_handlers():
        try:
            with _blocked_startup_signals():
                process = _start_process(command, project, child_environment, log_path)
                identity = _process_identity(process.pid)
                if not identity:
                    raise ExecutorRefused("memory service process identity could not be verified")
                digest = project_digest(project)
                observed_command = _process_command(process.pid)
                record = {
                    "pid": process.pid,
                    "port": port,
                    "project_digest": digest,
                    "project_dir": str(project),
                    "state_dir": str(state),
                    "process_identity": identity,
                    "service_identity": service_identity,
                    "token_file": str(token_file),
                    "command": command,
                    "observed_command": observed_command,
                    "final_command": _final_service_command(command, observed_command),
                    "holders": [holder],
                    "provisional": True,
                }
                _write_record(state, record)
            url = f"http://127.0.0.1:{port}/mcp"
            timeout = float(
                setup.get("memory_service_timeout", DEFAULT_READY_TIMEOUT_SECONDS)
            )
            health = _wait_ready(process, url, token, timeout)
            expected = {
                "project_digest": digest,
                "process_identity": identity,
                "service_identity": service_identity,
            }
            if health != expected:
                raise ExecutorRefused(
                    "memory service did not become ready with the bound project identity; "
                    f"inspect {log_path}"
                )
            record["observed_command"] = _process_command(process.pid)
            if not _service_process_matches(record):
                raise ExecutorRefused("memory service ready process command could not be verified")
            if not _holder_is_live(holder):
                raise ExecutorRefused("memory service holder identity is no longer live")
            record.pop("provisional", None)
            record.pop("final_command", None)
            _write_record(state, record)
            return _handle(record, project, holder["id"])
        except BaseException:
            with _blocked_startup_signals():
                if process is not None:
                    _stop_started_process(process)
                _remove_state_files(state)
            raise


@contextmanager
def _startup_signal_handlers() -> Iterator[None]:
    """Route startup interruption through the owned-process cleanup block."""
    if threading.current_thread() is not threading.main_thread():
        raise ExecutorRefused("memory service startup requires the main thread for signal cleanup")
    previous = {signum: signal.getsignal(signum) for signum in STARTUP_SIGNALS}

    def interrupt(signum: int, frame: Any) -> None:
        handler = previous[signum]
        if handler == signal.SIG_IGN:
            return
        if callable(handler):
            handler(signum, frame)
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    try:
        for signum in STARTUP_SIGNALS:
            signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def _blocked_startup_signals() -> Iterator[None]:
    """Defer interruption until startup state is durable or removed."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, STARTUP_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _wait_ready(
    process: Any, url: str, token: str, timeout: float
) -> dict[str, str] | None:
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if process.poll() is not None:
            return None
        health = _health(
            url,
            token,
            min(HEALTH_TIMEOUT_SECONDS, max(deadline - time.monotonic(), 0.01)),
        )
        if health is not None:
            return health
        if time.monotonic() >= deadline:
            return None
        time.sleep(READY_INTERVAL_SECONDS)


def _handle(record: Mapping[str, Any], project: Path, holder_id: str) -> ServiceHandle:
    return ServiceHandle(
        url=f"http://127.0.0.1:{int(record['port'])}/mcp",
        project_digest=str(record["project_digest"]),
        process_identity=str(record["process_identity"]),
        project_dir=project,
        holder_id=holder_id,
        service_identity=str(record["service_identity"]),
        token_file=Path(str(record["token_file"])),
    )


def _holder_pid(context: Any) -> int:
    value = getattr(context, "holder_pid", None)
    if value is None:
        raise ExecutorRefused("memory service requires an explicit holder pid")
    try:
        pid = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutorRefused("memory service holder pid is invalid") from exc
    if pid <= 0:
        raise ExecutorRefused("memory service holder pid is invalid")
    return pid


def _new_holder(pid: int) -> dict[str, Any]:
    identity = _process_identity(pid)
    if not identity:
        raise ExecutorRefused(
            "memory service holder process identity could not be verified; "
            "pass a live long-lived launcher pid"
        )
    return {"id": uuid.uuid4().hex, "pid": pid, "process_identity": identity}


def _holder_is_live(holder: Mapping[str, Any]) -> bool:
    """Return whether a previously verified holder still has the same start token."""
    return _holder_liveness(holder) == "live"


def _process_exists(pid: int) -> bool | None:
    """Return process existence, or ``None`` when the OS probe itself failed."""
    if sys.platform == "linux":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "pid=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env={"LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (
        result.returncode == 1
        and not result.stdout.strip()
        and not result.stderr.strip()
    ):
        return False
    if result.returncode == 0 and result.stdout.strip():
        return True
    return None


def _holder_liveness(
    holder: Mapping[str, Any],
    *,
    identity_probe: Any = None,
    existence_probe: Any = None,
) -> str:
    """Classify one holder with one shared definition for client and service."""
    pid = holder.get("pid")
    identity = holder.get("process_identity")
    if not holder.get("id") or not isinstance(pid, int) or not identity:
        return "dead"
    probe_identity = identity_probe or _process_identity
    actual = probe_identity(pid)
    if actual is not None:
        return "live" if actual == identity else "dead"
    probe_existence = existence_probe or _process_exists
    exists = probe_existence(pid)
    return "dead" if exists is False else "unknown"


def _active_holders(value: Any) -> list[dict[str, Any]]:
    """Keep verified-live and temporarily-unknown holders; drop only known-dead ones."""
    if not isinstance(value, list):
        return []
    active = []
    for holder in value:
        if not isinstance(holder, dict):
            continue
        if _holder_liveness(holder) == "dead":
            continue
        active.append(holder)
    return active


def _handle_matches(handle: ServiceHandle, record: Any) -> bool:
    return bool(
        isinstance(record, dict)
        and record.get("project_digest") == handle.project_digest
        and record.get("process_identity") == handle.process_identity
        and record.get("service_identity") == handle.service_identity
    )


def _discard_record(
    record: Mapping[str, Any], state: Path, expected_project_digest: str
) -> None:
    try:
        pid = int(record.get("pid", 0))
        expected = str(record.get("process_identity", ""))
    except (TypeError, ValueError):
        pid, expected = 0, ""
    signal_safe = (
        record.get("project_digest") == expected_project_digest
        and pid > 0
        and expected
        and _service_signal_safe(record)
    )
    if signal_safe:
        if not _terminate_owned(record):
            LOGGER.warning(
                "verified memory service record could not be reaped; process left untouched"
            )
    elif pid > 0:
        LOGGER.warning(
            "discarding unverifiable memory service record; pid %s left untouched",
            pid,
        )
    _remove_state_files(state)


def _remove_state_files(state: Path) -> None:
    for name in ("service.json", "bearer-token"):
        try:
            (state / name).unlink()
        except FileNotFoundError:
            pass


def _ensure_private_state_dir(state: Path) -> None:
    services = state.parent
    for directory in (services, state):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)


def _private_owned(path: Path, mode: int) -> bool:
    try:
        stat_result = path.stat()
    except OSError:
        return False
    return stat_result.st_uid == os.getuid() and stat_result.st_mode & 0o777 == mode


def _choose_port(requested: Any = None) -> int:
    if requested not in (None, "", 0, "0"):
        port = int(requested)
        if not 1 <= port <= 65535:
            raise ExecutorRefused("configured memory service port is invalid")
        return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_process(
    argv: list[str], cwd: Path, environment: Mapping[str, str], log_path: Path
) -> subprocess.Popen[bytes]:
    log = log_path.open("ab", buffering=0)
    try:
        return subprocess.Popen(
            [argv[0], "-I", "-c", _UNMASK_AND_EXEC, *argv],
            cwd=str(cwd),
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    finally:
        log.close()


def _process_identity(pid: int) -> str | None:
    return _verified_process_start_token(pid)


def _process_command(pid: int) -> tuple[str, ...] | str | None:
    """Read native argv boundaries, or preserve the complete ps output verbatim."""
    if sys.platform == "linux":
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        if not raw or not raw.endswith(b"\0"):
            return None
        return tuple(os.fsdecode(arg) for arg in raw[:-1].split(b"\0"))
    if sys.platform == "darwin":
        command = _macos_process_command(pid)
        if command is not None:
            return command
    return _ps_process_command(pid)


def _macos_process_command(pid: int) -> tuple[str, ...] | None:
    """Read KERN_PROCARGS2 without exposing the following environment bytes."""
    try:
        sysctl = ctypes.CDLL(None, use_errno=True).sysctl
        sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        ]
        sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid.
        size = ctypes.c_size_t(os.sysconf("SC_ARG_MAX"))
        buffer = ctypes.create_string_buffer(size.value)
        if sysctl(mib, len(mib), buffer, ctypes.byref(size), None, 0) != 0:
            return None
        return _argv_from_procargs(buffer.raw[:size.value])
    except (AttributeError, OSError, ValueError):
        return None


def _argv_from_procargs(raw: bytes) -> tuple[str, ...] | None:
    """Decode argc, executable-path padding and exactly argc native arguments."""
    width = ctypes.sizeof(ctypes.c_int)
    argc = int.from_bytes(raw[:width], sys.byteorder, signed=True)
    if len(raw) < width or argc <= 0:
        return None
    try:
        start = raw.index(b"\0", width) + 1
    except ValueError:
        return None
    while raw[start:start + 1] == b"\0":
        start += 1
    arguments = raw[start:].split(b"\0")
    if len(arguments) <= argc:
        return None
    return tuple(os.fsdecode(arg) for arg in arguments[:argc])


def _ps_process_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            timeout=1,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return os.fsdecode(result.stdout)


def _process_is_stopped(pid: int) -> bool:
    """Return whether ``pid`` is suspended, avoiding doomed health waits."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    status = result.stdout.strip()
    return result.returncode == 0 and status.startswith("T")


def _listener_owned_by_pid(pid: int, port: int) -> bool:
    """Bind authenticated health to ``pid`` when the platform exposes ownership."""
    lsof = Path("/usr/sbin/lsof")
    if not lsof.is_file():
        return True
    try:
        result = subprocess.run(
            [
                str(lsof),
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fp",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode == 0 and f"p{pid}" in result.stdout.splitlines()


def _final_service_command(
    command: list[str], observed: tuple[str, ...] | str | None
) -> tuple[str, ...] | str | None:
    """Remove the bootstrap, retaining the interpreter's native re-exec name."""
    if isinstance(observed, tuple):
        if observed[1:] == ("-I", "-c", _UNMASK_AND_EXEC, *command):
            return (observed[0], *command[1:])
    elif isinstance(observed, str):
        bootstrap = " -I -c " + _UNMASK_AND_EXEC + " " + " ".join(command) + "\n"
        if observed.endswith(bootstrap):
            return observed[:-len(bootstrap)] + " " + " ".join(command[1:]) + "\n"
    return observed


def _full_service_command_matches(record: Mapping[str, Any]) -> bool:
    """Require the exact observed argv and canonical project-bound service suffix."""
    try:
        pid = int(record["pid"])
        project = Path(str(record["project_dir"])).expanduser().resolve()
        digest = str(record["project_digest"])
        recorded = record["observed_command"]
    except (KeyError, TypeError, ValueError, OSError):
        return False
    if project_digest(project) != digest:
        return False
    service_suffix = (
        "-I",
        repository_runner_path(),
        "--memory-http",
        "--project-dir",
        str(project),
    )
    suffixes = [service_suffix]
    state_value = record.get("state_dir")
    if state_value:
        try:
            state = Path(str(state_value)).expanduser().resolve()
        except OSError:
            return False
        suffixes = [(*service_suffix, "--state-dir", str(state))]
    candidates = [recorded]
    if record.get("provisional") is True:
        candidates.append(record.get("final_command"))
    live_command = _process_command(pid)
    for candidate in candidates:
        if isinstance(candidate, (list, tuple)):
            candidate = tuple(candidate)
            bound = any(
                len(candidate) > len(suffix) and candidate[-len(suffix):] == suffix
                for suffix in suffixes
            )
        elif isinstance(candidate, str):
            bound = any(
                candidate.endswith(" " + " ".join(suffix) + "\n")
                for suffix in suffixes
            )
        else:
            continue
        if bound and candidate == live_command:
            return True
    return False


def _service_process_matches(record: Mapping[str, Any]) -> bool:
    try:
        pid = int(record["pid"])
        expected_identity = str(record["process_identity"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        _process_identity(pid) == expected_identity
        and _full_service_command_matches(record)
    )


def _service_signal_safe(record: Mapping[str, Any]) -> bool:
    """Require the recorded start token and exact project-bound command."""
    try:
        pid = int(record["pid"])
        identity = str(record["process_identity"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        _process_identity(pid) == identity and _full_service_command_matches(record)
    )


def _stop_started_process(process: Any) -> None:
    """Stop a process object created by this acquisition, even without a start token."""
    try:
        process.terminate()
        process.wait(timeout=2)
        return
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=2)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        pass


def _terminate_owned(record: Mapping[str, Any]) -> bool:
    if not _service_signal_safe(record):
        return False
    pid = int(record["pid"])
    expected_identity = str(record["process_identity"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _process_identity(pid) != expected_identity or _reap_if_exited(pid):
            return True
        time.sleep(0.05)
    if _process_identity(pid) != expected_identity or _reap_if_exited(pid):
        return True
    if not _service_signal_safe(record):
        return False
    os.kill(pid, signal.SIGKILL)
    return True


def _reap_if_exited(pid: int) -> bool:
    """Treat an exited, not yet reaped service as stopped; reap it when it is our child.

    After SIGTERM the process may linger as a zombie whose start token still matches
    while its argv is gone, so the exact-command check can no longer succeed.
    """
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return True
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().startswith("Z")


def _health(
    url: str, token: str, timeout: float = HEALTH_TIMEOUT_SECONDS
) -> dict[str, str] | None:
    health_url = url.rsplit("/mcp", 1)[0] + "/health"
    request = urllib.request.Request(
        health_url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    if not isinstance(payload, dict):
        return None
    keys = ("project_digest", "process_identity", "service_identity")
    if not all(isinstance(payload.get(key), str) for key in keys):
        return None
    return {key: payload[key] for key in keys}


def main(argv: list[str] | None = None) -> int:
    """Release an encoded handle for the Bash launcher."""
    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or arguments[0] != "release":
        print(
            "usage: python -m compchem_memory.harness.memory_service release HANDLE",
            file=sys.stderr,
        )
        return 2
    release_memory_service(decode_handle(arguments[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
