"""Authenticated loopback HTTP entry point for the project-bound memory app."""

from __future__ import annotations

import argparse
import hmac
import logging
import math
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import JSONResponse

from compchem_memory.harness import memory_service as service_state
from compchem_memory.harness.memory_service import (
    TOKEN_ENV,
    _process_identity,
    project_digest,
)


WATCHDOG_ENV = "MAGNOLIA_MEMORY_SERVICE_WATCHDOG_S"
GRACE_ENV = "MAGNOLIA_MEMORY_SERVICE_GRACE_S"
ACQUIRER_PID_ENV = "MAGNOLIA_MEMORY_SERVICE_ACQUIRER_PID"
ACQUIRER_IDENTITY_ENV = "MAGNOLIA_MEMORY_SERVICE_ACQUIRER_IDENTITY"
DEFAULT_WATCHDOG_SECONDS = 2.0
DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_UNKNOWN_LIMIT_SECONDS = 120.0
MIN_WATCHDOG_SECONDS = 0.5
MAX_WATCHDOG_SECONDS = 30.0
MIN_GRACE_SECONDS = 5.0
MAX_GRACE_SECONDS = 600.0
LOGGER = logging.getLogger(__name__)


@dataclass
class ServiceWatchdog:
    """Decide when this service no longer has a valid owning record or holder."""

    project_dir: Path
    service_identity: str
    process_pid: int
    process_identity: str
    acquirer_pid: int
    acquirer_identity: str
    grace_seconds: float = DEFAULT_GRACE_SECONDS
    unknown_limit_seconds: float = DEFAULT_UNKNOWN_LIMIT_SECONDS
    state_dir: Path | None = None
    now: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    last_holder_at: float = field(init=False)
    unknown_since: float | None = field(init=False, default=None)
    unknown_limit_logged: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.project_dir = self.project_dir.expanduser().resolve()
        if self.state_dir is None:
            self.state_dir = service_state.private_state_dir(self.project_dir)
        else:
            self.state_dir = self.state_dir.expanduser().resolve()
        self.started_at = self.now()
        self.last_holder_at = self.started_at

    def should_exit(self) -> bool:
        """Return true once the record or verified-holder contract is lost."""
        state = self.state_dir
        assert state is not None
        with service_state._state_lock(state):
            record = service_state._read_record(state)
            if record is None or not self._record_names_self(record):
                return True
            self._refresh_provisional_record(state, record)
            return self._ownership_expired(record)

    def remove_owned_state(self) -> bool:
        """Remove only this service's still-current record and bearer token."""
        state = self.state_dir
        assert state is not None
        with service_state._state_lock(state):
            record = service_state._read_record(state)
            if record is None or not self._record_names_self(record):
                return False
            service_state._remove_state_files(state)
            return True

    def _record_names_self(self, record: dict[str, Any]) -> bool:
        try:
            recorded_project = Path(str(record["project_dir"])).expanduser().resolve()
            recorded_pid = int(record["pid"])
        except (KeyError, TypeError, ValueError, OSError):
            return False
        return bool(
            recorded_project == self.project_dir
            and record.get("project_digest") == project_digest(self.project_dir)
            and recorded_pid == self.process_pid
            and record.get("process_identity") == self.process_identity
            and record.get("service_identity") == self.service_identity
        )

    def _refresh_provisional_record(
        self, state: Path, record: dict[str, Any]
    ) -> None:
        if record.get("provisional") is not True:
            return
        live_command = service_state._process_command(self.process_pid)
        recorded_command = record.get("observed_command")
        if isinstance(live_command, tuple) and isinstance(recorded_command, list):
            recorded_command = tuple(recorded_command)
        if live_command is None or recorded_command == live_command:
            return
        record["pid"] = self.process_pid
        record["process_identity"] = self.process_identity
        record["observed_command"] = live_command
        service_state._write_record(state, record)

    def _ownership_expired(self, record: dict[str, Any]) -> bool:
        checked_at = self.now()
        holder_states = self._holder_states(record.get("holders"))
        if "live" in holder_states:
            self.last_holder_at = checked_at
            self._clear_unknown()
        elif "unknown" in holder_states:
            if self._unknown_expired(checked_at, "holder"):
                return True
        else:
            self._clear_unknown()
            if checked_at - self.last_holder_at >= self.grace_seconds:
                return True
        provisional_expired = (
            record.get("provisional") is True
            and checked_at - self.started_at >= self.grace_seconds
        )
        if not provisional_expired:
            return False
        acquirer_state = self._acquirer_liveness()
        if acquirer_state == "dead":
            return True
        if acquirer_state == "unknown":
            return self._unknown_expired(checked_at, "acquirer")
        return False

    def _holder_states(self, holders: Any) -> set[str]:
        if not isinstance(holders, list):
            return set()
        return {
            service_state._holder_liveness(
                holder,
                identity_probe=_process_identity,
                existence_probe=service_state._process_exists,
            )
            for holder in holders
            if isinstance(holder, dict)
        }

    def _clear_unknown(self) -> None:
        self.unknown_since = None
        self.unknown_limit_logged = False

    def _unknown_expired(self, checked_at: float, subject: str) -> bool:
        if self.unknown_since is None:
            self.unknown_since = checked_at
        if checked_at - self.unknown_since < self.unknown_limit_seconds:
            return False
        if not self.unknown_limit_logged:
            LOGGER.warning(
                "memory service %s identity remained unknown for %.1f seconds; "
                "watchdog is failing closed",
                subject,
                self.unknown_limit_seconds,
            )
            self.unknown_limit_logged = True
        return True

    def _acquirer_liveness(self) -> str:
        if self.acquirer_pid <= 0 or not self.acquirer_identity:
            return "dead"
        actual = _process_identity(self.acquirer_pid)
        if actual is not None:
            return "live" if actual == self.acquirer_identity else "dead"
        exists = service_state._process_exists(self.acquirer_pid)
        return "dead" if exists is False else "unknown"


def _watchdog_loop(
    watchdog: ServiceWatchdog, interval: float, stop_event: threading.Event
) -> None:
    """Poll ownership and ask this process to stop without targeting another PID."""
    should_stop = False
    try:
        while not stop_event.wait(interval):
            try:
                should_stop = watchdog.should_exit()
            except BaseException:
                LOGGER.exception("memory service watchdog check failed; failing closed")
                should_stop = True
            if should_stop:
                break
    except BaseException:
        LOGGER.exception("memory service watchdog loop stopped unexpectedly; failing closed")
        should_stop = True
    is_set = getattr(stop_event, "is_set", lambda: False)
    if not should_stop or is_set():
        return
    remover = getattr(watchdog, "remove_owned_state", None)
    if callable(remover):
        try:
            remover()
        except BaseException:
            LOGGER.exception("memory service watchdog could not remove its owned state")
    signal.raise_signal(signal.SIGTERM)


def _seconds_from_env(
    name: str, default: float, *, minimum: float, maximum: float
) -> float:
    """Read and clamp one finite service timing value."""
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if math.isnan(value):
        raise RuntimeError(f"{name} must be a number")
    clamped = min(max(value, minimum), maximum)
    if clamped != value:
        LOGGER.warning(
            "%s value %s is out of range; clamping to %s (allowed %.1f-%.1f)",
            name,
            value,
            clamped,
            minimum,
            maximum,
        )
    return clamped


def _request_project_headers() -> tuple[str, ...]:
    """Return every project hint honoured by project resolution."""
    try:
        headers = get_http_headers(
            include={"x-project-dir", "x-magnolia-project-dir"}
        )
    except RuntimeError:
        return ()
    values = (
        headers.get("x-project-dir") or headers.get("X-Project-Dir"),
        headers.get("x-magnolia-project-dir")
        or headers.get("X-Magnolia-Project-Dir"),
    )
    return tuple(value for value in values if isinstance(value, str) and value.strip())


class ProjectBindingMiddleware(Middleware):
    """Reject cross-project HTTP messages before mounted tools or resources run."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.expanduser().resolve()

    def _validate(self, value: Any) -> None:
        if value in (None, ""):
            return
        target = Path(str(value)).expanduser().resolve()
        if target != self.project_dir:
            raise ToolError(
                f"memory HTTP service is bound to project {self.project_dir}; "
                "cross-project request refused"
            )

    def _validate_header(self) -> None:
        for value in _request_project_headers():
            self._validate(value)

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[Any]],
    ) -> Any:
        self._validate_header()
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[Any]],
    ) -> Any:
        self._validate_header()
        arguments = getattr(context.message, "arguments", None)
        if isinstance(arguments, dict):
            self._validate(arguments.get("project_dir"))
        return await call_next(context)

    async def check_resource_access(self, context: MiddlewareContext[Any]) -> None:
        self._validate_header()


def health_payload(project_dir: Path, process_identity: str) -> dict[str, str]:
    """Return the public readiness identity for one bound service."""
    return {
        "project_digest": project_digest(project_dir),
        "process_identity": process_identity,
        "service_identity": os.environ.get("MAGNOLIA_MEMORY_SERVICE_IDENTITY", ""),
    }


def build_server(token: str) -> FastMCP:
    """Wrap the existing memory app with bearer authentication and health."""
    if not token:
        raise RuntimeError("memory HTTP service requires a bearer token")
    from compchem_memory.server import mcp as memory_app

    verifier = StaticTokenVerifier(
        {token: {"client_id": "magnolia-memory", "scopes": []}}
    )
    app = FastMCP("compchem-memory-http", auth=verifier)
    if os.environ.get("MAGNOLIA_PROJECT_BOUND") == "1":
        project = os.environ.get("MAGNOLIA_PROJECT_DIR")
        if not project:
            raise RuntimeError("memory HTTP service requires a bound project directory")
        app.add_middleware(ProjectBindingMiddleware(Path(project)))
    app.mount(memory_app)

    @app.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        identity = _process_identity(os.getpid())
        if not identity:
            return JSONResponse({"error": "identity unavailable"}, status_code=503)
        project = Path(os.environ["MAGNOLIA_PROJECT_DIR"]).resolve()
        return JSONResponse(health_payload(project, identity))

    return app


def main(argv: list[str] | None = None) -> None:
    """Run one authenticated streamable-HTTP service on IPv4 loopback only."""
    parser = argparse.ArgumentParser(prog="python -m compchem_memory.memory_http")
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("MAGNOLIA_PROJECT_BOUND") != "1":
        raise RuntimeError("memory HTTP service requires strict project binding")
    bound = Path(os.environ.get("MAGNOLIA_PROJECT_DIR", "")).expanduser().resolve()
    if args.project_dir.expanduser().resolve() != bound:
        raise RuntimeError("memory HTTP service project argument differs from its binding")
    token = os.environ.get(TOKEN_ENV, "")
    port = int(os.environ.get("MAGNOLIA_MEMORY_SERVICE_PORT", "0"))
    if port <= 0:
        raise RuntimeError("memory HTTP service requires a configured port")
    process_identity = _process_identity(os.getpid())
    if not process_identity:
        raise RuntimeError("memory HTTP service process identity is unavailable")
    service_identity = os.environ.get("MAGNOLIA_MEMORY_SERVICE_IDENTITY", "")
    acquirer_identity = os.environ.get(ACQUIRER_IDENTITY_ENV, "")
    try:
        acquirer_pid = int(os.environ.get(ACQUIRER_PID_ENV, "0"))
    except ValueError as exc:
        raise RuntimeError("memory HTTP service acquirer pid is invalid") from exc
    if not service_identity or acquirer_pid <= 0 or not acquirer_identity:
        raise RuntimeError("memory HTTP service supervision identity is incomplete")
    interval = _seconds_from_env(
        WATCHDOG_ENV,
        DEFAULT_WATCHDOG_SECONDS,
        minimum=MIN_WATCHDOG_SECONDS,
        maximum=MAX_WATCHDOG_SECONDS,
    )
    grace = _seconds_from_env(
        GRACE_ENV,
        DEFAULT_GRACE_SECONDS,
        minimum=MIN_GRACE_SECONDS,
        maximum=MAX_GRACE_SECONDS,
    )
    watchdog = ServiceWatchdog(
        bound,
        service_identity=service_identity,
        process_pid=os.getpid(),
        process_identity=process_identity,
        acquirer_pid=acquirer_pid,
        acquirer_identity=acquirer_identity,
        grace_seconds=grace,
        state_dir=args.state_dir,
    )
    stop_event = threading.Event()
    watcher = threading.Thread(
        target=_watchdog_loop,
        args=(watchdog, interval, stop_event),
        name="magnolia-memory-watchdog",
        daemon=True,
    )
    watcher.start()
    try:
        build_server(token).run(
            transport="streamable-http", host="127.0.0.1", port=port, show_banner=False
        )
    finally:
        stop_event.set()
        watcher.join(timeout=max(1.0, interval * 2))


if __name__ == "__main__":
    main()
