import asyncio
import json
import os
import signal
import socket
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from compchem_memory import memory_http, server
from compchem_memory.harness import memory_service
from compchem_memory.harness.memory_service import (
    ensure_memory_service,
    release_memory_service,
    token_for_handle,
)
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.exceptions import ToolError
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp import types as mt
from types import SimpleNamespace


@pytest.fixture(autouse=True)
def _private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _watchdog_record(project, *, holders=None, provisional=False):
    state = memory_service.private_state_dir(project)
    memory_service._ensure_private_state_dir(state)
    record = {
        "pid": 4101,
        "port": 43101,
        "project_digest": memory_service.project_digest(project),
        "project_dir": str(project.resolve()),
        "process_identity": "ps:4101:service",
        "service_identity": "service-id",
        "token_file": str(state / "bearer-token"),
        "holders": list(holders or []),
    }
    if provisional:
        record["provisional"] = True
    memory_service._write_record(state, record)
    return state, record


def _watchdog(project, clock):
    return memory_http.ServiceWatchdog(
        project,
        service_identity="service-id",
        process_pid=4101,
        process_identity="ps:4101:service",
        acquirer_pid=4100,
        acquirer_identity="ps:4100:acquirer",
        grace_seconds=5,
        now=lambda: clock[0],
    )


def test_memory_http_refuses_missing_or_wrong_bearer():
    app = memory_http.build_server("service-token")

    assert asyncio.run(app.auth.verify_token("wrong-token")) is None
    assert asyncio.run(app.auth.verify_token("service-token")) is not None


def test_memory_http_two_projects_are_isolated(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert memory_http.health_payload(first, "proc:1:first") != memory_http.health_payload(
        second, "proc:2:second"
    )


def test_service_exits_when_record_removed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    state, _record = _watchdog_record(project)
    (state / "service.json").unlink()
    monkeypatch.setattr(
        memory_http, "_process_identity", lambda pid: f"ps:{pid}:live"
    )

    assert _watchdog(project, [0.0]).should_exit() is True
    (state / "service.json").write_text("{not-json", encoding="utf-8")
    os.chmod(state / "service.json", 0o600)
    assert _watchdog(project, [0.0]).should_exit() is True


def test_service_exits_when_record_names_other_pid(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    state, record = _watchdog_record(project)
    record["pid"] = 9999
    memory_service._write_record(state, record)
    monkeypatch.setattr(
        memory_http,
        "_process_identity",
        lambda pid: "ps:4101:service" if pid == 4101 else None,
    )

    assert _watchdog(project, [0.0]).should_exit() is True
    record["pid"] = 4101
    record["process_identity"] = "ps:4101:other"
    memory_service._write_record(state, record)
    assert _watchdog(project, [0.0]).should_exit() is True
    record["process_identity"] = "ps:4101:service"
    record["project_digest"] = "other-project"
    memory_service._write_record(state, record)
    assert _watchdog(project, [0.0]).should_exit() is True


def test_service_stays_while_holder_alive(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    holder = {"id": "holder", "pid": 4200, "process_identity": "ps:4200:holder"}
    state, record = _watchdog_record(project, holders=[holder], provisional=True)
    record["observed_command"] = ["/wrapper", "-m", "compchem_memory.memory_http"]
    memory_service._write_record(state, record)
    identities = {
        4100: "ps:4100:acquirer",
        4101: "ps:4101:service",
        4200: "ps:4200:holder",
    }
    monkeypatch.setattr(memory_http, "_process_identity", identities.get)
    live_command = ("/framework/Python", "-m", "compchem_memory.memory_http")
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: live_command)
    clock = [20.0]
    watchdog = _watchdog(project, clock)

    assert watchdog.should_exit() is False
    refreshed = json.loads((state / "service.json").read_text())
    assert refreshed["observed_command"] == list(live_command)
    clock[0] = 200.0
    assert watchdog.should_exit() is False


def test_acquirer_unknown_ceiling_fires_even_while_holder_is_live(
    tmp_path, monkeypatch, caplog
):
    project = tmp_path / "project"
    project.mkdir()
    holder = {"id": "holder", "pid": 4200, "process_identity": "ps:4200:holder"}
    _watchdog_record(project, holders=[holder], provisional=True)
    identities = {4101: "ps:4101:service", 4200: "ps:4200:holder"}
    monkeypatch.setattr(memory_http, "_process_identity", identities.get)
    monkeypatch.setattr(
        memory_service,
        "_process_exists",
        lambda pid: None if pid == 4100 else pid in identities,
    )
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: None)
    clock = [0.0]
    watchdog = _watchdog(project, clock)
    watchdog.unknown_limit_seconds = 10.0

    assert watchdog.should_exit() is False
    clock[0] = 5.0
    assert watchdog.should_exit() is False
    clock[0] = 14.0
    assert watchdog.should_exit() is False
    clock[0] = 15.0
    with caplog.at_level("WARNING", logger=memory_http.LOGGER.name):
        assert watchdog.should_exit() is True
    assert "acquirer" in (watchdog.exit_reason or "")
    assert "stopping the service" in caplog.text


@pytest.mark.parametrize("provisional", [True, False])
def test_dead_acquirer_ends_only_provisional_service_with_live_holder(
    tmp_path, monkeypatch, provisional
):
    project = tmp_path / "project"
    project.mkdir()
    holder = {"id": "holder", "pid": 4200, "process_identity": "ps:4200:holder"}
    _watchdog_record(project, holders=[holder], provisional=provisional)
    identities = {4101: "ps:4101:service", 4200: "ps:4200:holder"}
    monkeypatch.setattr(memory_http, "_process_identity", identities.get)
    monkeypatch.setattr(
        memory_service, "_process_exists", lambda pid: pid in identities
    )
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: None)
    clock = [0.0]
    watchdog = _watchdog(project, clock)

    assert watchdog.should_exit() is False
    clock[0] = 5.0
    assert watchdog.should_exit() is provisional


def test_each_unknown_holder_gets_its_own_ceiling(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    holders = [
        {"id": "first", "pid": 4200, "process_identity": "ps:4200:first"},
        {"id": "second", "pid": 4300, "process_identity": "ps:4300:second"},
    ]
    _watchdog_record(project, holders=holders)
    monkeypatch.setattr(
        memory_http,
        "_process_identity",
        lambda pid: "ps:4101:service" if pid == 4101 else None,
    )
    monkeypatch.setattr(memory_service, "_process_exists", lambda pid: None)
    clock = [0.0]
    watchdog = _watchdog(project, clock)
    watchdog.unknown_limit_seconds = 10.0

    assert watchdog.should_exit() is False
    assert watchdog.holder_unknown_since == {"first": 0.0, "second": 0.0}
    clock[0] = 10.0
    assert watchdog.should_exit() is True


def test_service_exits_after_last_holder_dies_and_grace(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    holder = {"id": "holder", "pid": 4200, "process_identity": "ps:4200:holder"}
    _watchdog_record(project, holders=[holder])
    identities = {
        4100: "ps:4100:acquirer",
        4101: "ps:4101:service",
        4200: "ps:4200:holder",
    }
    monkeypatch.setattr(memory_http, "_process_identity", identities.get)
    monkeypatch.setattr(memory_service, "_process_exists", lambda pid: pid in identities)
    clock = [10.0]
    watchdog = _watchdog(project, clock)

    assert watchdog.should_exit() is False
    identities.pop(4200)
    clock[0] = 14.9
    assert watchdog.should_exit() is False
    clock[0] = 15.0
    assert watchdog.should_exit() is True


def test_watchdog_never_signals_other_processes(monkeypatch):
    raised = []
    removed = []

    class StopEvent:
        def wait(self, interval):
            return False

    class Watchdog:
        def should_exit(self):
            return True

        def remove_owned_state(self):
            removed.append(True)
            return True

    monkeypatch.setattr(memory_http.signal, "raise_signal", raised.append)
    monkeypatch.setattr(
        memory_http.os,
        "kill",
        lambda *args: pytest.fail(f"watchdog signalled an external pid: {args}"),
    )

    memory_http._watchdog_loop(Watchdog(), 0.01, StopEvent())

    assert raised == [signal.SIGTERM]
    assert removed == [True]


def test_watchdog_forces_self_exit_after_graceful_shutdown_deadline(monkeypatch):
    raised = []
    forced = []

    class StopEvent:
        def wait(self, interval):
            return False

        def is_set(self):
            return False

    class Watchdog:
        def should_exit(self):
            return True

    class ForcedExit(Exception):
        pass

    monkeypatch.setattr(memory_http.signal, "raise_signal", raised.append)

    def force_exit(code):
        forced.append(code)
        raise ForcedExit

    monkeypatch.setattr(memory_http.os, "_exit", force_exit)

    with pytest.raises(ForcedExit):
        memory_http._watchdog_loop(
            Watchdog(), 0.01, StopEvent(), hard_exit_seconds=0.01
        )

    assert raised == [signal.SIGTERM]
    assert forced == [1]


def test_memory_http_refuses_noncanonical_state_dir_without_side_effects(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(mode=0o755)
    unrelated.chmod(0o755)
    wrong_state = unrelated / memory_service.project_digest(project)
    monkeypatch.setenv("MAGNOLIA_PROJECT_BOUND", "1")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(project))
    monkeypatch.setenv(memory_http.TOKEN_ENV, "token")
    monkeypatch.setenv("MAGNOLIA_MEMORY_SERVICE_PORT", "43101")
    monkeypatch.setenv("MAGNOLIA_MEMORY_SERVICE_IDENTITY", "service-id")
    monkeypatch.setenv(memory_http.ACQUIRER_PID_ENV, "4100")
    monkeypatch.setenv(memory_http.ACQUIRER_IDENTITY_ENV, "ps:4100:acquirer")
    monkeypatch.setattr(memory_http, "_process_identity", lambda pid: "ps:4101:service")
    monkeypatch.setattr(
        memory_http,
        "build_server",
        lambda token: pytest.fail("server built before state-dir validation"),
    )

    with pytest.raises(RuntimeError, match="state directory"):
        memory_http.main(
            ["--project-dir", str(project), "--state-dir", str(wrong_state)]
        )

    assert unrelated.stat().st_mode & 0o777 == 0o755
    assert not wrong_state.exists()


def test_watchdog_wait_failure_takes_service_down(monkeypatch):
    raised = []

    class StopEvent:
        def wait(self, interval):
            raise RuntimeError("watchdog wait failed")

        def is_set(self):
            return False

    monkeypatch.setattr(memory_http.signal, "raise_signal", raised.append)

    memory_http._watchdog_loop(object(), 0.01, StopEvent())

    assert raised == [signal.SIGTERM]


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("locked state"),
        IsADirectoryError("service.lock is a directory"),
        OSError(28, "no space left on device"),
    ],
)
def test_watchdog_fails_closed_when_state_check_raises(monkeypatch, failure):
    raised = []

    class StopEvent:
        def wait(self, interval):
            return False

        def is_set(self):
            return False

    class Watchdog:
        def should_exit(self):
            raise failure

    monkeypatch.setattr(memory_http.signal, "raise_signal", raised.append)

    memory_http._watchdog_loop(Watchdog(), 0.01, StopEvent())

    assert raised == [signal.SIGTERM]


def test_watchdog_fails_closed_when_provisional_refresh_fills_disk(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    _state, record = _watchdog_record(project, provisional=True)
    record["observed_command"] = ["/wrapper", "-m", "compchem_memory.memory_http"]
    memory_service._write_record(_state, record)
    monkeypatch.setattr(
        memory_http,
        "_process_identity",
        lambda pid: {
            4100: "ps:4100:acquirer",
            4101: "ps:4101:service",
        }.get(pid),
    )
    monkeypatch.setattr(
        memory_service,
        "_process_command",
        lambda pid: ("/framework/Python", "-m", "compchem_memory.memory_http"),
    )
    monkeypatch.setattr(
        memory_service,
        "_write_record",
        lambda *args: (_ for _ in ()).throw(OSError(28, "no space left on device")),
    )
    raised = []

    class StopEvent:
        def wait(self, interval):
            return False

        def is_set(self):
            return False

    monkeypatch.setattr(memory_http.signal, "raise_signal", raised.append)

    memory_http._watchdog_loop(_watchdog(project, [0.0]), 0.01, StopEvent())

    assert raised == [signal.SIGTERM]


@pytest.mark.parametrize("slow", [False, True])
def test_failed_or_slow_holder_probe_is_unknown_until_hard_limit(
    tmp_path, monkeypatch, slow, caplog
):
    project = tmp_path / "project"
    project.mkdir()
    holder = {"id": "holder", "pid": 4200, "process_identity": "ps:4200:holder"}
    _watchdog_record(project, holders=[holder])

    def identity(pid):
        if pid == 4200 and slow:
            time.sleep(0.02)
        if pid == 4200:
            return None
        return {4100: "ps:4100:acquirer", 4101: None}.get(pid)

    monkeypatch.setattr(memory_http, "_process_identity", identity)
    monkeypatch.setattr(memory_service, "_process_exists", lambda pid: True)
    clock = [0.0]
    watchdog = _watchdog(project, clock)
    watchdog.unknown_limit_seconds = 120.0

    assert watchdog.should_exit() is False
    clock[0] = 6.0
    assert watchdog.should_exit() is False
    clock[0] = 119.9
    assert watchdog.should_exit() is False
    clock[0] = 120.0
    assert watchdog.should_exit() is True
    assert "unknown" in caplog.text.lower()


def test_holder_without_id_uses_same_definition_on_both_sides(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    holder = {"pid": 4200, "process_identity": "ps:4200:holder"}
    _watchdog_record(project, holders=[holder])
    monkeypatch.setattr(
        memory_http,
        "_process_identity",
        lambda pid: {
            4100: "ps:4100:acquirer",
            4101: "ps:4101:service",
            4200: "ps:4200:holder",
        }.get(pid),
    )
    clock = [0.0]
    watchdog = _watchdog(project, clock)

    assert memory_service._holder_liveness(holder) == "dead"
    assert watchdog.should_exit() is False
    clock[0] = 5.0
    assert watchdog.should_exit() is True


def test_watchdog_removes_state_only_when_record_still_names_self(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    state, record = _watchdog_record(project)
    (state / "bearer-token").write_text("token\n", encoding="utf-8")
    os.chmod(state / "bearer-token", 0o600)
    monkeypatch.setattr(memory_http, "_process_identity", lambda pid: None)
    watchdog = _watchdog(project, [0.0])

    assert watchdog.remove_owned_state() is True
    assert not (state / "service.json").exists()
    assert not (state / "bearer-token").exists()

    memory_service._write_record(state, {**record, "service_identity": "replacement"})
    (state / "bearer-token").write_text("replacement-token\n", encoding="utf-8")
    os.chmod(state / "bearer-token", 0o600)

    assert watchdog.remove_owned_state() is False
    assert (state / "service.json").exists()
    assert (state / "bearer-token").exists()


def test_watchdog_uses_acquirer_state_dir_when_service_home_differs(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    state, _record = _watchdog_record(project)
    other_home = tmp_path / "service-home"
    other_home.mkdir()
    monkeypatch.setenv("HOME", str(other_home))
    monkeypatch.setattr(
        memory_http,
        "_process_identity",
        lambda pid: {4100: "ps:4100:acquirer"}.get(pid),
    )
    watchdog = memory_http.ServiceWatchdog(
        project,
        service_identity="service-id",
        process_pid=4101,
        process_identity="ps:4101:service",
        acquirer_pid=4100,
        acquirer_identity="ps:4100:acquirer",
        state_dir=state,
        now=lambda: 0.0,
    )

    assert watchdog.should_exit() is False
    assert not (other_home / ".magnolia").exists()


@pytest.mark.parametrize(
    ("name", "raw", "default", "minimum", "maximum", "expected"),
    [
        (memory_http.WATCHDOG_ENV, "0.01", 2.0, 0.5, 30.0, 0.5),
        (memory_http.WATCHDOG_ENV, "300", 2.0, 0.5, 30.0, 30.0),
        (memory_http.GRACE_ENV, "1", 10.0, 5.0, 600.0, 5.0),
        (memory_http.GRACE_ENV, "1000", 10.0, 5.0, 600.0, 600.0),
    ],
)
def test_service_timings_are_clamped_and_logged(
    monkeypatch, caplog, name, raw, default, minimum, maximum, expected
):
    monkeypatch.setenv(name, raw)

    assert memory_http._seconds_from_env(
        name, default, minimum=minimum, maximum=maximum
    ) == expected
    assert "clamp" in caplog.text.lower()


def test_memory_http_rejects_cross_project_request(tmp_path, monkeypatch):
    bound = tmp_path / "bound"
    other = tmp_path / "other"
    bound.mkdir()
    other.mkdir()
    monkeypatch.setattr(server, "PROJECT_DIR", str(bound))
    monkeypatch.setenv("MAGNOLIA_PROJECT_BOUND", "1")

    assert server._resolve_project_store(str(bound)) == str(bound.resolve())
    with pytest.raises(server.ProjectBindingError, match="bound to a different project"):
        server._resolve_project_store(str(other))

    monkeypatch.setattr(
        server.project_context,
        "effective_project_dir",
        lambda explicit=None: explicit or str(other),
    )
    with pytest.raises(server.ProjectBindingError, match="bound to a different project"):
        server._resolve_project_store()


def test_project_binding_middleware_refuses_every_tool_before_dispatch(
    tmp_path, monkeypatch
):
    bound = tmp_path / "bound"
    other = tmp_path / "other"
    bound.mkdir()
    other.mkdir()
    store = other / ".magnolia"
    store.mkdir()
    for relative, body in {
        "sessions/sentinel.jsonl": b"session-before\n",
        "staging/sentinel.md": b"staging-before\n",
        "distill.log": b"distill-before\n",
        ".distill-notices": b"notices-before\n",
    }.items():
        path = store / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    before = {path.relative_to(store): path.read_bytes() for path in store.rglob("*") if path.is_file()}
    middleware = memory_http.ProjectBindingMiddleware(bound)
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    dispatched = []

    async def call_next(context):
        dispatched.append(context.message.name)
        return None

    async def exercise():
        for name, tool in tools.items():
            properties = tool.parameters.get("properties", {})
            assert "project_dir" in properties, name
            context = MiddlewareContext(
                message=mt.CallToolRequestParams(
                    name=name,
                    arguments={"project_dir": str(other)},
                ),
                method="tools/call",
            )
            with pytest.raises(ToolError, match="bound to project"):
                await middleware.on_call_tool(context, call_next)

    asyncio.run(exercise())

    after = {path.relative_to(store): path.read_bytes() for path in store.rglob("*") if path.is_file()}
    assert len(tools) == 26
    assert dispatched == []
    assert after == before


def test_project_binding_middleware_refuses_header_and_resource_before_dispatch(
    tmp_path, monkeypatch
):
    bound = tmp_path / "bound"
    other = tmp_path / "other"
    bound.mkdir()
    other.mkdir()
    middleware = memory_http.ProjectBindingMiddleware(bound)
    monkeypatch.setattr(
        memory_http, "_request_project_headers", lambda: (str(other),)
    )
    dispatched = []

    async def call_next(context):
        dispatched.append(context.method)
        return None

    async def exercise():
        request = MiddlewareContext(
            message=mt.ListResourcesRequest(),
            method="resources/list",
        )
        resource = MiddlewareContext(
            message=mt.ReadResourceRequestParams(uri="magnolia://goal"),
            method="resources/read",
        )
        with pytest.raises(ToolError, match="bound to project"):
            await middleware.on_request(request, call_next)
        with pytest.raises(ToolError, match="bound to project"):
            await middleware.on_read_resource(resource, call_next)

    asyncio.run(exercise())
    assert dispatched == []


def test_project_binding_middleware_validates_every_honoured_header(
    tmp_path, monkeypatch
):
    bound = tmp_path / "bound"
    other = tmp_path / "other"
    bound.mkdir()
    other.mkdir()
    middleware = memory_http.ProjectBindingMiddleware(bound)
    monkeypatch.setattr(
        memory_http,
        "_request_project_headers",
        lambda: (str(bound), str(other)),
    )
    dispatched = []

    async def call_next(context):
        dispatched.append(context.method)

    context = MiddlewareContext(
        message=mt.ListToolsRequest(),
        method="tools/list",
    )
    with pytest.raises(ToolError, match=str(bound.resolve())):
        asyncio.run(middleware.on_request(context, call_next))
    assert dispatched == []


def test_strict_http_binding_ignores_active_project_marker(tmp_path, monkeypatch):
    bound = tmp_path / "bound"
    other = tmp_path / "other"
    marker = bound / ".magnolia" / ".active-project"
    marker.parent.mkdir(parents=True)
    other.mkdir()
    marker.write_text(str(other), encoding="utf-8")
    monkeypatch.setattr(server.project_context, "_STARTUP_ENV", str(bound))
    monkeypatch.setattr(server.project_context, "_request_hint", lambda: None)
    monkeypatch.setattr(
        server.project_context, "_active_project_hint", lambda startup: str(other)
    )
    monkeypatch.setattr(server.project_context, "_process_start_time", lambda: -1.0)
    monkeypatch.setenv("MAGNOLIA_PROJECT_BOUND", "1")
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(bound))

    assert server.project_context.effective_project_dir() == str(bound)


def test_real_loopback_service_auth_and_release(tmp_path, monkeypatch):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        pytest.skip(f"loopback bind is unavailable in this environment: {exc}")
    finally:
        probe.close()
    home = tmp_path / "home"
    project = tmp_path / "project"
    other = tmp_path / "other"
    home.mkdir(exist_ok=True)
    project.mkdir()
    other.mkdir()
    monkeypatch.setenv("HOME", str(home))
    environment = dict(os.environ)
    environment["DEEPSEEK_API_KEY"] = "loopback-test-sentinel"
    context = SimpleNamespace(
        project_dir=project,
        python=sys.executable,
        holder_pid=os.getpid(),
        setup={"memory_provider": "deepseek", "memory_model": "test"},
        environment=environment,
    )
    handle = ensure_memory_service(context)
    try:
        request = urllib.request.Request(
            handle.url.replace("/mcp", "/health"),
            headers={"Authorization": f"Bearer {token_for_handle(handle)}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["project_digest"] == handle.project_digest
        assert payload["service_identity"] == handle.service_identity

        async def cross_project_call():
            transport = StreamableHttpTransport(
                handle.url,
                headers={
                    "Authorization": f"Bearer {token_for_handle(handle)}",
                },
            )
            async with Client(transport) as client:
                return await client.call_tool(
                    "memory_get_goal",
                    {"project_dir": str(other)},
                    raise_on_error=False,
                )

        refused = asyncio.run(cross_project_call())
        message = " ".join(
            getattr(item, "text", "") for item in refused.content
        )
        assert refused.is_error is True
        assert "bound to project" in message
        assert str(project.resolve()) in message
        assert str(other.resolve()) not in message
    finally:
        assert release_memory_service(handle) is True
