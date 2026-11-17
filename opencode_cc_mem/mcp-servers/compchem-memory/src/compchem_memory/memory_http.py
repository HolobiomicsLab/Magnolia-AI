"""Authenticated loopback HTTP entry point for the project-bound memory app."""

from __future__ import annotations

import argparse
import asyncio
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
from fastmcp.server.auth import AccessToken, TokenVerifier
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
from compchem_memory.tool_catalog import (
    MEMORY_TOOL_NAMES,
    memory_tool_requires_lease,
    resolve_memory_tool_environment,
)
from compchem_memory.writer_lease import run_with_project_writer_lease


WATCHDOG_ENV = "MAGNOLIA_MEMORY_SERVICE_WATCHDOG_S"
GRACE_ENV = "MAGNOLIA_MEMORY_SERVICE_GRACE_S"
ACQUIRER_PID_ENV = "MAGNOLIA_MEMORY_SERVICE_ACQUIRER_PID"
ACQUIRER_IDENTITY_ENV = "MAGNOLIA_MEMORY_SERVICE_ACQUIRER_IDENTITY"
DEFAULT_WATCHDOG_SECONDS = 2.0
DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_UNKNOWN_LIMIT_SECONDS = 120.0
DEFAULT_HARD_EXIT_SECONDS = 5.0
MIN_WATCHDOG_SECONDS = 0.5
MAX_WATCHDOG_SECONDS = 30.0
MIN_GRACE_SECONDS = 5.0
MAX_GRACE_SECONDS = 600.0
LOGGER = logging.getLogger(__name__)


class ConstantTimeTokenVerifier(TokenVerifier):
    """Verify the one service bearer without data-dependent string lookup."""

    def __init__(self, expected_token: str) -> None:
        super().__init__(required_scopes=[])
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return fixed service claims only after constant-time comparison."""
        if not constant_time_token_matches(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="magnolia-memory",
            scopes=[],
            claims={"client_id": "magnolia-memory", "scopes": []},
        )


class ToolAllowlistMiddleware(Middleware):
    """Hide and refuse every memory tool outside one resolved allowlist."""

    def __init__(self, allowed_tools: tuple[str, ...]) -> None:
        self.allowed_tools = frozenset(allowed_tools)

    async def on_list_tools(self, context, call_next):
        """Return only tools this server instance may dispatch."""
        tools = await call_next(context)
        return tuple(tool for tool in tools if tool.name in self.allowed_tools)

    async def on_call_tool(self, context, call_next):
        """Refuse direct calls that bypass discovery."""
        name = getattr(context.message, "name", "")
        if name not in self.allowed_tools:
            raise ToolError(f"memory tool is not allowed by this role: {name}")
        return await call_next(context)

    @property
    def exposes_full_catalog(self) -> bool:
        """Return whether non-tool surfaces may retain legacy full access."""
        return self.allowed_tools == MEMORY_TOOL_NAMES

    async def on_list_resources(self, context, call_next):
        """Hide resources from every restricted role."""
        return await call_next(context) if self.exposes_full_catalog else ()

    async def on_list_resource_templates(self, context, call_next):
        """Hide resource templates from every restricted role."""
        return await call_next(context) if self.exposes_full_catalog else ()

    async def on_list_prompts(self, context, call_next):
        """Hide prompts from every restricted role."""
        return await call_next(context) if self.exposes_full_catalog else ()

    async def on_read_resource(self, context, call_next):
        """Refuse direct resource reads by restricted roles."""
        if not self.exposes_full_catalog:
            raise ToolError("memory resource is not allowed by this role")
        return await call_next(context)

    async def on_get_prompt(self, context, call_next):
        """Refuse direct prompt reads by restricted roles."""
        if not self.exposes_full_catalog:
            raise ToolError("memory prompt is not allowed by this role")
        return await call_next(context)


class ProjectWriterLeaseMiddleware(Middleware):
    """Serialize durable memory mutations with a process-safe project lease."""

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()

    async def on_call_tool(self, context, call_next):
        """Lease mutating calls while allowing read calls to overlap."""
        name = getattr(context.message, "name", "")
        arguments = getattr(context.message, "arguments", None)
        if not memory_tool_requires_lease(name, arguments):
            return await call_next(context)
        return await run_with_project_writer_lease(
            self.project_dir, lambda: call_next(context)
        )


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
    holder_unknown_since: dict[str, float] = field(init=False, default_factory=dict)
    holder_unknown_logged: set[str] = field(init=False, default_factory=set)
    acquirer_unknown_since: float | None = field(init=False, default=None)
    acquirer_unknown_logged: bool = field(init=False, default=False)
    lock_unknown_since: float | None = field(init=False, default=None)
    exit_reason: str | None = field(init=False, default=None)

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
        try:
            with service_state._state_lock(state, blocking=False):
                self.lock_unknown_since = None
                record = service_state._read_record(state)
                if record is None:
                    return self._expire("the service record is gone")
                if not self._record_names_self(record):
                    return self._expire("the service record names another service")
                self._refresh_provisional_record(state, record)
                return self._ownership_expired(record)
        except BlockingIOError:
            return self._lock_unknown_expired()

    def _expire(self, reason: str) -> bool:
        self.exit_reason = reason
        LOGGER.warning("memory service watchdog stopping the service: %s", reason)
        return True

    def remove_owned_state(self) -> bool:
        """Remove only this service's still-current record and bearer token."""
        state = self.state_dir
        assert state is not None
        try:
            with service_state._state_lock(state, blocking=False):
                record = service_state._read_record(state)
                if record is None or not self._record_names_self(record):
                    return False
                service_state._remove_state_files(state)
                return True
        except BlockingIOError:
            return False

    def _lock_unknown_expired(self) -> bool:
        """Bound lock contention by the same ceiling as unknown owners."""
        checked_at = self.now()
        if self.lock_unknown_since is None:
            self.lock_unknown_since = checked_at
        elapsed = checked_at - self.lock_unknown_since
        if elapsed < self.unknown_limit_seconds:
            return False
        return self._expire(
            "the service lock stayed unavailable for "
            f"{self.unknown_limit_seconds:.1f} seconds; watchdog is failing closed"
        )

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
        has_live_holder = any(state == "live" for _key, state in holder_states)
        unknown_holders = [
            key for key, state in holder_states if state == "unknown"
        ]
        current_keys = {key for key, _state in holder_states}
        for key in tuple(self.holder_unknown_since):
            if key not in current_keys or key not in unknown_holders:
                self._clear_holder_unknown(key)
        if has_live_holder:
            self.last_holder_at = checked_at
        unknown_expirations = [
            self._holder_unknown_expired(key, checked_at) for key in unknown_holders
        ]
        holders_unknown_expired = bool(unknown_expirations) and all(
            unknown_expirations
        )
        holders_dead_expired = (
            not has_live_holder
            and not unknown_holders
            and checked_at - self.last_holder_at >= self.grace_seconds
        )
        provisional_expired = (
            record.get("provisional") is True
            and checked_at - self.started_at >= self.grace_seconds
        )
        acquirer_expired = False
        if provisional_expired:
            acquirer_state = self._acquirer_liveness()
            if acquirer_state == "dead":
                acquirer_expired = True
                self._clear_acquirer_unknown()
            elif acquirer_state == "unknown":
                acquirer_expired = self._acquirer_unknown_expired(checked_at)
            else:
                self._clear_acquirer_unknown()
        else:
            self._clear_acquirer_unknown()
        # A provisional record never reached readiness: its holders were never
        # handed the service, so a lost acquirer ends it even if a holder lives.
        if acquirer_expired:
            return self._expire(
                "the provisional record's acquirer is gone or unverifiable"
            )
        if has_live_holder:
            return False
        if holders_unknown_expired:
            return self._expire(
                "every holder stayed unverifiable past the ceiling; "
                "watchdog is failing closed"
            )
        if holders_dead_expired:
            return self._expire("no live holder remained past the grace period")
        return False

    def _holder_states(self, holders: Any) -> list[tuple[str, str]]:
        if not isinstance(holders, list):
            return []
        states = []
        for index, holder in enumerate(holders):
            if not isinstance(holder, dict):
                continue
            key = str(holder.get("id") or f"invalid-{index}")
            states.append(
                (
                    key,
                    service_state._holder_liveness(
                        holder,
                        identity_probe=_process_identity,
                        existence_probe=service_state._process_exists,
                    ),
                )
            )
        return states

    def _clear_holder_unknown(self, key: str) -> None:
        self.holder_unknown_since.pop(key, None)
        self.holder_unknown_logged.discard(key)

    def _holder_unknown_expired(self, key: str, checked_at: float) -> bool:
        unknown_since = self.holder_unknown_since.setdefault(key, checked_at)
        if checked_at - unknown_since < self.unknown_limit_seconds:
            return False
        if key not in self.holder_unknown_logged:
            LOGGER.warning(
                "memory service holder %s identity remained unknown for %.1f seconds",
                key,
                self.unknown_limit_seconds,
            )
            self.holder_unknown_logged.add(key)
        return True

    def _clear_acquirer_unknown(self) -> None:
        self.acquirer_unknown_since = None
        self.acquirer_unknown_logged = False

    def _acquirer_unknown_expired(self, checked_at: float) -> bool:
        if self.acquirer_unknown_since is None:
            self.acquirer_unknown_since = checked_at
        if checked_at - self.acquirer_unknown_since < self.unknown_limit_seconds:
            return False
        if not self.acquirer_unknown_logged:
            LOGGER.warning(
                "memory service acquirer identity remained unknown for %.1f seconds; "
                "watchdog is failing closed",
                self.unknown_limit_seconds,
            )
            self.acquirer_unknown_logged = True
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
    watchdog: ServiceWatchdog,
    interval: float,
    stop_event: threading.Event,
    *,
    hard_exit_seconds: float | None = None,
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
            if remover() is False:
                LOGGER.warning("memory service watchdog could not remove its owned state")
        except BaseException:
            LOGGER.exception("memory service watchdog could not remove its owned state")
    signal.raise_signal(signal.SIGTERM)
    if hard_exit_seconds is not None and not stop_event.wait(hard_exit_seconds):
        LOGGER.error(
            "memory service did not stop within %.1f seconds; forcing self-exit",
            hard_exit_seconds,
        )
        os._exit(1)


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


def constant_time_token_matches(supplied: str, expected: str) -> bool:
    """Compare arbitrary Unicode bearer text as bytes without raising."""
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def build_server(token: str) -> FastMCP:
    """Wrap the existing memory app with bearer authentication and health."""
    if not token:
        raise RuntimeError("memory HTTP service requires a bearer token")
    from compchem_memory.server import mcp as memory_app

    try:
        allowlist = resolve_memory_tool_environment(os.environ, legacy_stdio=False)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not allowlist.server_names:
        raise RuntimeError("memory HTTP service requires a non-empty tool allowlist")
    verifier = ConstantTimeTokenVerifier(token)
    app = FastMCP("compchem-memory-http", auth=verifier)
    app.add_middleware(ToolAllowlistMiddleware(allowlist.server_names))
    if os.environ.get("MAGNOLIA_PROJECT_BOUND") == "1":
        project = os.environ.get("MAGNOLIA_PROJECT_DIR")
        if not project:
            raise RuntimeError("memory HTTP service requires a bound project directory")
        app.add_middleware(ProjectBindingMiddleware(Path(project)))
        app.add_middleware(ProjectWriterLeaseMiddleware(Path(project)))
    app.mount(memory_app)

    @app.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        supplied = request.headers.get("authorization", "")
        if not constant_time_token_matches(supplied, f"Bearer {token}"):
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
    supplied_state = args.state_dir.expanduser()
    canonical_state = supplied_state.resolve()
    try:
        allowlist = resolve_memory_tool_environment(os.environ, legacy_stdio=False)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    expected_state = service_state.private_state_dir(bound, allowlist.neutral_names)
    if supplied_state != canonical_state or canonical_state != expected_state:
        raise RuntimeError(
            "memory HTTP service state directory must be the canonical "
            "~/.magnolia/services/<project-digest> path"
        )
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
        kwargs={"hard_exit_seconds": DEFAULT_HARD_EXIT_SECONDS},
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
