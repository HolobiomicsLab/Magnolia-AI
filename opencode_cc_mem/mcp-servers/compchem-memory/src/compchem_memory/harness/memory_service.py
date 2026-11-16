"""Project-bound lifecycle for the loopback compchem-memory HTTP service."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    from campaign_runner.executors.registry import ExecutorRefused
except ImportError:  # Standalone memory-server installation.
    class ExecutorRefused(RuntimeError):
        """Raised when a safe memory-service launch cannot be prepared."""

try:
    from campaign_runner.governor import _verified_process_start_token
except ImportError:  # Standalone memory-server installation.
    def _verified_process_start_token(pid: int) -> str | None:
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
                env={"LC_ALL": "C", "TZ": "UTC"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        started = " ".join(result.stdout.split())
        return f"ps:{pid}:{started}" if result.returncode == 0 and started else None


TOKEN_ENV = "MAGNOLIA_MEMORY_SERVICE_TOKEN"
DEFAULT_READY_TIMEOUT_SECONDS = 10.0
READY_INTERVAL_SECONDS = 0.1
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
    return project_dir.expanduser().resolve() / ".magnolia" / "private" / "memory-service"


def ensure_memory_service(context: Any) -> ServiceHandle:
    """Acquire a ready, authenticated service bound to ``context.project_dir``."""
    project = _context_project(context)
    environment = _server_environment(context)
    _require_provider_key(context, environment)
    state = private_state_dir(project)
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    with _state_lock(state):
        record = _read_record(state)
        if record is not None:
            handle = _reuse(record, state, project)
            if handle is not None:
                return handle
            _discard_record(record, state)
        return _start_service(context, project, state, environment)


def release_memory_service(handle: ServiceHandle) -> bool:
    """Release one holder and stop only its verified service when references reach zero."""
    state = private_state_dir(handle.project_dir)
    with _state_lock(state):
        record = _read_record(state)
        if not _handle_matches(handle, record):
            return False
        holders = [
            holder
            for holder in _live_holders(record.get("holders", []))
            if holder.get("id") != handle.holder_id
        ]
        if holders:
            record["holders"] = holders
            _write_record(state, record)
            return False
        stopped = _terminate_owned(
            int(record["pid"]), str(record["process_identity"])
        )
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
    supplied = getattr(context, "environment", None)
    base = os.environ if supplied is None else supplied
    return {str(key): str(value) for key, value in base.items()}


def _require_provider_key(context: Any, environment: Mapping[str, str]) -> None:
    setup = getattr(context, "setup", {}) or {}
    provider = str(
        setup.get("memory_provider") or environment.get("MAGNOLIA_MEMORY_PROVIDER") or ""
    ).strip().lower()
    fallback_names = tuple(
        name for row in PROVIDER_KEYS.values() for name in row
    )
    names = PROVIDER_KEYS.get(provider, fallback_names)
    if any(environment.get(name, "").strip() for name in names):
        return
    suffix = f" for provider '{provider}'" if provider else ""
    raise ExecutorRefused(
        "memory service requires a provider API key"
        f"{suffix} in the server environment; configure Magnolia memory before launch"
    )


@contextmanager
def _state_lock(state: Path) -> Iterator[None]:
    state.mkdir(parents=True, exist_ok=True)
    path = state / "service.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_record(state: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((state / "service.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_record(state: Path, record: Mapping[str, Any]) -> None:
    path = state / "service.json"
    temporary = state / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _reuse(
    record: dict[str, Any], state: Path, project: Path
) -> ServiceHandle | None:
    expected_digest = project_digest(project)
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
    if _process_identity(pid) != identity:
        return None
    try:
        if token_file.stat().st_mode & 0o777 != 0o600:
            return None
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    url = f"http://127.0.0.1:{port}/mcp"
    health = _health(url, token)
    if health != {
        "project_digest": expected_digest,
        "process_identity": identity,
        "service_identity": service_identity,
    }:
        return None
    holder = _new_holder()
    record["holders"] = [*_live_holders(record.get("holders", [])), holder]
    _write_record(state, record)
    return _handle(record, project, holder["id"])


def _start_service(
    context: Any,
    project: Path,
    state: Path,
    environment: dict[str, str],
) -> ServiceHandle:
    setup = getattr(context, "setup", {}) or {}
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
            "MAGNOLIA_PROJECT_DIR": str(project),
            "MAGNOLIA_PROJECT_BOUND": "1",
            "MAGNOLIA_MEMORY_SERVICE_PORT": str(port),
            "MAGNOLIA_MEMORY_SERVICE_IDENTITY": service_identity,
            TOKEN_ENV: token,
        }
    )
    process = _start_process(
        [python, "-m", "compchem_memory.memory_http"],
        project,
        child_environment,
        log_path,
    )
    identity = _process_identity(process.pid)
    if not identity:
        _remove_state_files(state)
        raise ExecutorRefused("memory service process identity could not be verified")
    digest = project_digest(project)
    url = f"http://127.0.0.1:{port}/mcp"
    timeout = float(setup.get("memory_service_timeout", DEFAULT_READY_TIMEOUT_SECONDS))
    health = _wait_ready(process, url, token, timeout)
    expected = {
        "project_digest": digest,
        "process_identity": identity,
        "service_identity": service_identity,
    }
    if health != expected:
        _terminate_owned(process.pid, identity)
        _remove_state_files(state)
        raise ExecutorRefused(
            "memory service did not become ready with the bound project identity; "
            f"inspect {log_path}"
        )
    holder = _new_holder()
    record = {
        "pid": process.pid,
        "port": port,
        "project_digest": digest,
        "process_identity": identity,
        "service_identity": service_identity,
        "token_file": str(token_file),
        "holders": [holder],
    }
    _write_record(state, record)
    return _handle(record, project, holder["id"])


def _wait_ready(
    process: Any, url: str, token: str, timeout: float
) -> dict[str, str] | None:
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if process.poll() is not None:
            return None
        health = _health(url, token)
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


def _new_holder() -> dict[str, Any]:
    pid = os.getpid()
    return {"id": uuid.uuid4().hex, "pid": pid, "process_identity": _process_identity(pid)}


def _live_holders(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    live = []
    for holder in value:
        if not isinstance(holder, dict) or not holder.get("id"):
            continue
        pid = holder.get("pid")
        expected = holder.get("process_identity")
        if isinstance(pid, int) and expected and _process_identity(pid) != expected:
            continue
        live.append(holder)
    return live


def _handle_matches(handle: ServiceHandle, record: Any) -> bool:
    return bool(
        isinstance(record, dict)
        and record.get("project_digest") == handle.project_digest
        and record.get("process_identity") == handle.process_identity
        and record.get("service_identity") == handle.service_identity
    )


def _discard_record(record: Mapping[str, Any], state: Path) -> None:
    try:
        pid = int(record.get("pid", 0))
        expected = str(record.get("process_identity", ""))
    except (TypeError, ValueError):
        pid, expected = 0, ""
    if pid > 0 and expected and _process_identity(pid) == expected:
        _terminate_owned(pid, expected)
    _remove_state_files(state)


def _remove_state_files(state: Path) -> None:
    for name in ("service.json", "bearer-token"):
        try:
            (state / name).unlink()
        except FileNotFoundError:
            pass


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
            argv,
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


def _terminate_owned(pid: int, expected_identity: str) -> bool:
    if _process_identity(pid) != expected_identity:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _process_identity(pid) != expected_identity:
            return True
        time.sleep(0.05)
    if _process_identity(pid) != expected_identity:
        return True
    os.kill(pid, signal.SIGKILL)
    return True


def _health(url: str, token: str) -> dict[str, str] | None:
    health_url = url.rsplit("/mcp", 1)[0] + "/health"
    request = urllib.request.Request(
        health_url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
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
