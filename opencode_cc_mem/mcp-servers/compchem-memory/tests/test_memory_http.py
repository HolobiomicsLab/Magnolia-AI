import asyncio
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

from compchem_memory import memory_http, server
from compchem_memory.harness.memory_service import (
    ensure_memory_service,
    release_memory_service,
    token_for_handle,
)
from fastmcp.server.middleware import MiddlewareContext
from mcp import types as mt
from types import SimpleNamespace


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
            with pytest.raises(server.ProjectBindingError):
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
    monkeypatch.setattr(memory_http, "_request_project_header", lambda: str(other))
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
        with pytest.raises(server.ProjectBindingError):
            await middleware.on_request(request, call_next)
        with pytest.raises(server.ProjectBindingError):
            await middleware.on_read_resource(resource, call_next)

    asyncio.run(exercise())
    assert dispatched == []


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
    home.mkdir()
    project.mkdir()
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
    finally:
        assert release_memory_service(handle) is True
