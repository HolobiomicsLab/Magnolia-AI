"""Per-project opencode v2 service management."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .process import stop_process_group
from .security import redact_file


class ServiceError(Exception):
    """Raised when the dedicated opencode service cannot be used."""


def _record_path(project_dir: Path) -> Path:
    return project_dir / ".magnolia" / "campaign-service.json"


def _log_path(project_dir: Path) -> Path:
    return project_dir / ".magnolia" / "campaign-service.log"


def read_service(project_dir: str | Path) -> dict[str, Any] | None:
    try:
        value = json.loads(_record_path(Path(project_dir)).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_service(project_dir: Path, value: dict[str, Any]) -> None:
    path = _record_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _get(port: int, path: str, password: str | None) -> tuple[int, bytes]:
    """GET a local service path; (HTTP status, body), or (0, b"") when unreachable."""
    headers = {}
    if password is not None:
        credentials = base64.b64encode(f"opencode:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 0, b""


def _healthy(port: int, password: str) -> bool:
    """The service answers an API path with the password and refuses it without.

    Observed on OpenCode 2.3.0: `GET /` serves the web interface with status 200
    whatever the credentials, so it cannot tell a healthy, password-protected
    service apart. `/api/session` answers 401 without the right password."""
    if _get(port, "/api/session", password)[0] != 200:
        return False
    return _get(port, "/api/session", None)[0] == 401


def _listed_agents(port: int, password: str) -> set[str]:
    status, body = _get(port, "/api/agent", password)
    if status != 200:
        return set()
    try:
        data = json.loads(body.decode("utf-8")).get("data")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(a["id"]) for a in data if isinstance(a, dict) and a.get("id")}


def _wait_for_agents(
    port: int,
    password: str,
    names: Sequence[str],
    timeout_seconds: float,
    interval: float = 0.5,
) -> bool:
    """Wait until every named agent is listed by the service.

    Observed on OpenCode 2.3.0: right after `serve` answers, `/api/agent` lists
    no agent; the full list follows about two seconds later."""
    wanted = set(names)
    deadline = time.monotonic() + timeout_seconds
    while True:
        if wanted <= _listed_agents(port, password):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _config_digest(config_content: str | None) -> str | None:
    if config_content is None:
        return None
    return hashlib.sha256(config_content.encode("utf-8")).hexdigest()


def _agent_steps(config_content: str | None) -> dict[str, int]:
    try:
        config = json.loads(config_content or "{}")
    except json.JSONDecodeError:
        return {}
    agents = config.get("agents") if isinstance(config, dict) else None
    if not isinstance(agents, dict):
        return {}
    steps = {}
    for name, value in agents.items():
        cap = value.get("steps") if isinstance(value, dict) else None
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
            steps[str(name)] = cap
    return steps


def merge_agent_steps(config_content: str | None, steps: dict[str, int]) -> str:
    """OPENCODE_CONFIG_CONTENT with `agents.<name>.steps` set for each agent.

    Observed on OpenCode 2.3.0: a dedicated service reads this override only
    from its own environment at start; set on a client attached with --server
    it has no effect. Other keys of the given configuration are kept."""
    try:
        config = json.loads(config_content or "{}")
    except json.JSONDecodeError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    agents = config.get("agents")
    if not isinstance(agents, dict):
        agents = {}
        config["agents"] = agents
    for name, limit in steps.items():
        entry = agents.get(name)
        if not isinstance(entry, dict):
            entry = {}
            agents[name] = entry
        entry["steps"] = limit
    return json.dumps(config, sort_keys=True)


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _choose_port() -> int:
    requested = os.environ.get("MAGNOLIA_CAMPAIGN_SERVICE_PORT")
    if requested:
        return int(requested)
    for port in range(4120, 4200):
        if _port_available(port):
            return port
    raise ServiceError("no free port in range 4120-4199")


def service_status(project_dir: str | Path) -> dict[str, Any]:
    record = read_service(project_dir)
    if record is None:
        return {"running": False}
    try:
        pid = int(record.get("pid"))
        port = int(record.get("port"))
        password = str(record.get("password"))
    except (TypeError, ValueError):
        return {"running": False, "error": "invalid service record"}
    running = _pid_alive(pid) and _healthy(port, password)
    return {**record, "password": "<hidden>", "running": running}


def ensure_service(
    project_dir: str | Path,
    binary: str,
    db_path: str,
    timeout_seconds: float = 30.0,
    config_content: str | None = None,
    required_agents: Sequence[str] = (),
) -> dict[str, Any]:
    """Start (or reuse) the project's dedicated service.

    `config_content`, when given, is the service's OPENCODE_CONFIG_CONTENT; a
    running service started with a different one is refused, like one with a
    different database. `required_agents` must all be listed by the service
    before it is returned."""
    project = Path(project_dir).resolve()
    digest = _config_digest(config_content)
    record = read_service(project)
    if record is not None:
        status = service_status(project)
        if status.get("running"):
            stored_db = str(record.get("db") or "")
            requested_db = str(db_path)
            if (
                not stored_db
                or Path(stored_db).expanduser().resolve()
                != Path(requested_db).expanduser().resolve()
                or record.get("project") != project.name
            ):
                raise ServiceError(
                    "the running dedicated service uses a different database or "
                    "project; run magnolia-campaign service stop before switching"
                )
            if digest is not None and record.get("config_sha256") != digest:
                raise ServiceError(
                    "the running dedicated service uses a different configuration "
                    "(agent step limits); run magnolia-campaign service stop first"
                )
            return {**record, "running": True}
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError):
            pid = 0
        if not _pid_alive(pid):
            stop_service(project)
        else:
            raise ServiceError(
                "a service process is alive but not healthy; run "
                "magnolia-campaign service stop before starting a new one"
            )
    password = os.environ.get("OPENCODE_SERVER_PASSWORD") or secrets.token_urlsafe(24)
    port = _choose_port()
    log_path = _log_path(project)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "OPENCODE_DB": db_path,
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_SERVER_USERNAME": "opencode",
            "PWD": str(project),
        }
    )
    if config_content is not None:
        environment["OPENCODE_CONFIG_CONTENT"] = config_content
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [
                binary,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(project),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    record = {
        "project": project.name,
        "port": port,
        "db": db_path,
        "pid": process.pid,
        "pgid": process.pid,
        "password": password,
        "started_at": time.time(),
        "log": str(log_path),
    }
    if digest is not None:
        record["config_sha256"] = digest
        record["agent_steps"] = _agent_steps(config_content)
    _write_service(project, record)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _write_service(project, record)
            raise ServiceError(f"service exited early; inspect {log_path}")
        if _healthy(port, password):
            remaining = max(deadline - time.monotonic(), 0.0)
            if required_agents and not _wait_for_agents(
                port, password, required_agents, timeout_seconds=remaining
            ):
                _stop_process(record)
                try:
                    _record_path(project).unlink()
                except FileNotFoundError:
                    pass
                raise ServiceError(
                    "service did not list the agents "
                    f"{', '.join(required_agents)} within {timeout_seconds}s; "
                    "check that the project directory is a real directory under "
                    f"projects/ (inspect {log_path})"
                )
            redact_file(log_path)
            record["running"] = True
            return record
        time.sleep(0.5)
    _stop_process(record)
    try:
        _record_path(project).unlink()
    except FileNotFoundError:
        pass
    raise ServiceError(f"service did not become healthy within {timeout_seconds}s")


def stop_service(project_dir: str | Path) -> bool:
    project = Path(project_dir).resolve()
    record = read_service(project)
    if record is None:
        return False
    _stop_process(record)
    try:
        _record_path(project).unlink()
    except FileNotFoundError:
        pass
    return True


def _stop_process(record: dict[str, Any]) -> None:
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return
    try:
        stop_process_group(pid, grace_seconds=5.0)
    except ProcessLookupError:
        return
    except Exception as exc:
        raise ServiceError(f"cannot stop service process {pid}: {exc}") from exc
