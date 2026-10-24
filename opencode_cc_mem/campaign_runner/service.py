"""Per-project opencode v2 service management."""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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


def _healthy(port: int, password: str) -> bool:
    credentials = base64.b64encode(f"opencode:{password}".encode()).decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        headers={"Authorization": f"Basic {credentials}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


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
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
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
    _write_service(project, record)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _write_service(project, record)
            raise ServiceError(f"service exited early; inspect {log_path}")
        if _healthy(port, password):
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
