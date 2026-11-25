"""The HTTP transport gates every request when a token file is configured."""

import pytest

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from compchem_tools.server import _ToolServiceAuth, _run_http, mcp, uvicorn


def test_unset_token_file_preserves_the_http_runner(monkeypatch):
    monkeypatch.delenv("COMPCHEM_TOOLS_AUTH_TOKEN_FILE", raising=False)
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))
    _run_http("127.0.0.1", 8012)
    assert calls == [{"transport": "streamable-http", "host": "127.0.0.1", "port": 8012}]


def test_http_runner_installs_auth_on_the_mcp_app(monkeypatch, token_file):
    monkeypatch.setenv("COMPCHEM_TOOLS_AUTH_TOKEN_FILE", str(token_file))

    def run(app, *, host, port, **kwargs):
        assert (host, port) == ("127.0.0.1", 8012)
        with TestClient(app) as client:
            assert client.post("/mcp").status_code == 401
            assert client.get("/unregistered").status_code == 401
            response = client.post(
                "/mcp",
                headers={"Authorization": "Bearer first-token", "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
                }},
            )
            assert response.status_code == 200

    monkeypatch.setattr(uvicorn, "run", run)
    _run_http("127.0.0.1", 8012)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        for event in ("lifespan.startup", "lifespan.shutdown"):
            assert (await receive())["type"] == event
            await send({"type": event + ".complete"})
        return
    await JSONResponse({"ok": True})(scope, receive, send)


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "tools.token"
    path.write_bytes(b"first-token")
    return path


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Basic first-token")],
        [(b"authorization", b"bearer first-token")],
        [(b"authorization", b"Bearer wrong-token")],
        [(b"authorization", b"Bearer first-token ")],
        [(b"authorization", b"Bearer \xff")],
        [(b"authorization", b"Bearer first-token")] * 2,
    ],
)
def test_refuses_nonmatching_authorization(token_file, headers):
    with TestClient(_ToolServiceAuth(app, token_file)) as client:
        response = client.post("/mcp", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"error": "Unauthorized"}


def test_checks_the_current_token_on_each_request(token_file):
    with TestClient(_ToolServiceAuth(app, token_file)) as client:
        first = {"Authorization": "Bearer first-token"}
        assert client.post("/mcp", headers=first).status_code == 200
        replacement = token_file.with_suffix(".tmp")
        replacement.write_bytes(b"second-token")
        replacement.replace(token_file)
        assert client.delete("/mcp", headers=first).status_code == 401
        assert client.get("/mcp", headers={"Authorization": "Bearer second-token"}).status_code == 200
        token_file.write_bytes(b"")
        assert client.post("/mcp", headers={"Authorization": "Bearer "}).status_code == 401
        token_file.unlink()
        assert client.post("/mcp", headers=first).status_code == 401


@pytest.mark.parametrize("content", [b"first-token\n", b" \tfirst-token\r\n"])
def test_ignores_surrounding_token_file_whitespace(token_file, content):
    token_file.write_bytes(content)
    with TestClient(_ToolServiceAuth(app, token_file)) as client:
        assert client.post("/mcp", headers={"Authorization": "Bearer first-token"}).status_code == 200


def test_whitespace_only_token_file_fails_closed(token_file):
    token_file.write_bytes(b" \t")
    with TestClient(_ToolServiceAuth(app, token_file)) as client:
        response = client.post("/mcp", headers=[(b"authorization", b"Bearer  \t")])
    assert response.status_code == 401
