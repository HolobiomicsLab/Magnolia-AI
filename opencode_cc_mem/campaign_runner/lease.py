"""Per-attempt execution leases."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .process import stop_process, stop_process_group
from .state import CampaignBusyError


class StaleLeaseError(CampaignBusyError):
    """Raised when a lease exists but its owner is no longer alive."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def lease_path(run_dir: Path, stage: str, attempt: int) -> Path:
    return Path(run_dir) / "control" / "leases" / f"{stage}-{attempt}.json"


def read_lease(run_dir: Path, stage: str, attempt: int) -> dict[str, Any] | None:
    path = lease_path(run_dir, stage, attempt)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def list_leases(run_dir: Path) -> list[dict[str, Any]]:
    directory = Path(run_dir) / "control" / "leases"
    if not directory.is_dir():
        return []
    leases: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            leases.append(value)
    return leases


def _owner_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner_is_alive(lease: dict[str, Any]) -> bool:
    hostname = lease.get("hostname")
    if hostname and hostname != socket.gethostname():
        return True
    if not hostname:
        return False
    try:
        pid = int(lease.get("owner_pid"))
    except (TypeError, ValueError):
        return False
    return _owner_alive(pid)


@dataclass
class AttemptLease:
    run_dir: Path
    stage: str
    attempt: int
    path: Path
    data: dict[str, Any]

    @classmethod
    def acquire(cls, run_dir: Path, stage: str, attempt: int) -> "AttemptLease":
        path = lease_path(run_dir, stage, attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stage": stage,
            "attempt": attempt,
            "owner_pid": os.getpid(),
            "owner_pgid": os.getpgid(0),
            "hostname": socket.gethostname(),
            "started_at": _now(),
            "heartbeat_at": _now(),
        }
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(path, flags, 0o644)
        except FileExistsError as exc:
            existing = read_lease(run_dir, stage, attempt) or {}
            if owner_is_alive(existing):
                raise CampaignBusyError(
                    f"attempt lease is held by pid {existing.get('owner_pid')}: "
                    f"{stage}/{attempt}"
                ) from exc
            raise StaleLeaseError(
                f"stale attempt lease requires human review: {stage}/{attempt}"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return cls(run_dir, stage, attempt, path, payload)

    def update_pid(self, pid: int, pgid: int, session_id: str | None = None) -> None:
        self.data["worker_pid"] = pid
        self.data["worker_pgid"] = pgid
        if session_id:
            self.data["opencode_session_id"] = session_id
        self.heartbeat()

    def update_session(self, session_id: str | None) -> None:
        if session_id:
            self.data["opencode_session_id"] = session_id
            self.heartbeat()

    def heartbeat(self) -> None:
        self.data["heartbeat_at"] = _now()
        _atomic_json(self.path, self.data)

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def stop_active_leases(run_dir: Path, grace_seconds: float = 5.0) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for lease in list_leases(run_dir):
        if lease.get("hostname") != socket.gethostname():
            errors.append(
                {
                    "lease": f"{lease.get('stage')}/{lease.get('attempt')}",
                    "error": "lease is owned by another host",
                }
            )
            continue
        label = f"{lease.get('stage')}/{lease.get('attempt')}"
        try:
            owner_pid = int(lease.get("owner_pid") or 0)
        except (TypeError, ValueError):
            owner_pid = 0
        try:
            worker_pgid = int(lease.get("worker_pgid") or 0)
        except (TypeError, ValueError):
            worker_pgid = 0
        if worker_pgid <= 0 and owner_pid == os.getpid():
            continue
        try:
            if worker_pgid > 0:
                stop_process_group(worker_pgid, grace_seconds)
            elif owner_pid > 0:
                stop_process(owner_pid, grace_seconds)
            elif not _owner_alive(owner_pid):
                pass
            else:
                raise RuntimeError("lease has no valid pid/pgid")
        except Exception as exc:
            errors.append({"lease": label, "error": str(exc)})
            continue
        path = lease_path(run_dir, str(lease.get("stage")), int(lease.get("attempt")))
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return errors
