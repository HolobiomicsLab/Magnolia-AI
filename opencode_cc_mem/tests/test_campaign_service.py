import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.service import (  # noqa: E402
    ServiceError,
    ensure_service,
    read_service,
    service_status,
    stop_service,
)


def test_missing_service_is_not_running(tmp_path):
    assert service_status(tmp_path)["running"] is False
    assert stop_service(tmp_path) is False


def test_running_service_must_match_database(tmp_path, monkeypatch):
    project = tmp_path / "project"
    path = project / ".magnolia" / "campaign-service.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "project": "project",
                "port": 4112,
                "db": str(tmp_path / "old.db"),
                "pid": 1234,
                "pgid": 1234,
                "password": "secret",
                "started_at": 0,
                "log": "log",
            }
        )
    )
    monkeypatch.setattr(
        "campaign_runner.service.service_status",
        lambda project_dir: {"running": True},
    )
    try:
        ensure_service(project, "opencode", str(tmp_path / "new.db"))
    except ServiceError as exc:
        assert "different database" in str(exc)
    else:
        raise AssertionError("expected database mismatch to be rejected")


def test_service_status_hides_password_and_dead_pid(tmp_path):
    path = tmp_path / ".magnolia" / "campaign-service.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "project": "project",
                "port": 4999,
                "db": "db",
                "pid": 99999999,
                "pgid": 99999999,
                "password": "secret-token",
                "started_at": 0,
                "log": "log",
            }
        )
    )
    record = read_service(tmp_path)
    assert record["password"] == "secret-token"
    status = service_status(tmp_path)
    assert status["running"] is False
    assert status["password"] == "<hidden>"


def test_managed_cap_proof_rejects_stale_service_record(tmp_path, monkeypatch):
    from campaign_runner.executors.contracts import ExecContext
    from campaign_runner.executors.registry import ExecutorRefused, _service_cap_checker
    from campaign_runner.routing import ResolvedRoute

    project = tmp_path / "project"
    path = project / ".magnolia" / "campaign-service.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "project": project.name,
        "project_path": str(project.resolve()),
        "port": 4120,
        "pid": 99999999,
        "password": "old",
        "agent_steps": {"campaign-worker": 5},
        "config_sha256": "0" * 64,
    }))
    context = ExecContext(
        route=ResolvedRoute("opencode", "m", "f", "k", "worker", "test"),
        role="worker",
        project_dir=project,
        opencode_server="http://127.0.0.1:4120",
        governor_cap=5,
        agent="campaign-worker",
    )

    with pytest.raises(ExecutorRefused, match="live|authenticated"):
        _service_cap_checker(context, "campaign-worker")


# ---- behaviour observed on OpenCode 2.3.0 ------------------------------------
# `GET /` answers 200 (the web interface) with no password, a wrong password or
# the right one; `/api/...` answers 401 without the right password. Right after
# `serve` starts, `/api/agent` lists no agent, then the full list ~2 s later.
# `agents.<name>.steps` in OPENCODE_CONFIG_CONTENT reaches a dedicated service
# only when it is in the service's environment at start, not the client's.

import base64  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from campaign_runner import service as service_module  # noqa: E402


def _fake_v2_server(password, *, enforce=True, agents_after=0,
                    agents=("build", "campaign-worker", "campaign-reviewer")):
    state = {"agent_calls": 0}
    expected = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload, content_type="application/json"):
            body = payload.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, "<html></html>", "text/html")
                return
            if enforce and self.headers.get("Authorization") != expected:
                self._send(401, '{"_tag":"UnauthorizedError"}')
                return
            if self.path == "/api/session":
                self._send(200, '{"data":[]}')
            elif self.path == "/api/agent":
                state["agent_calls"] += 1
                listed = agents if state["agent_calls"] > agents_after else ()
                self._send(200, json.dumps({"data": [{"id": a} for a in listed]}))
            else:
                self._send(404, "{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def test_health_check_requires_the_password():
    server, _ = _fake_v2_server("right")
    try:
        port = server.server_address[1]
        assert service_module._healthy(port, "right") is True
        assert service_module._healthy(port, "wrong") is False
    finally:
        server.shutdown()


def test_health_check_rejects_a_service_that_needs_no_password():
    server, _ = _fake_v2_server("right", enforce=False)
    try:
        assert service_module._healthy(server.server_address[1], "right") is False
    finally:
        server.shutdown()


def test_wait_for_agents_until_listed():
    server, state = _fake_v2_server("right", agents_after=2)
    try:
        port = server.server_address[1]
        assert service_module._wait_for_agents(
            port, "right", ["campaign-worker"], timeout_seconds=5, interval=0.05)
        assert state["agent_calls"] == 3
    finally:
        server.shutdown()


def test_wait_for_agents_times_out():
    server, _ = _fake_v2_server("right", agents=("build",))
    try:
        assert not service_module._wait_for_agents(
            server.server_address[1], "right", ["campaign-worker"],
            timeout_seconds=0.3, interval=0.05)
    finally:
        server.shutdown()


class _FakeProcess:
    pid = 424242

    def poll(self):
        return None


def test_service_starts_with_config_content_and_waits_for_agents(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        seen["stdin"] = kwargs.get("stdin")
        return _FakeProcess()

    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service_module, "_choose_port", lambda: 4137)
    monkeypatch.setattr(service_module, "_healthy", lambda port, password: True)
    monkeypatch.setattr(
        service_module, "_wait_for_agents",
        lambda port, password, names, timeout_seconds, interval=0.5:
            seen.setdefault("agents", list(names)) is not None)
    monkeypatch.setattr(service_module, "redact_file", lambda path: None)
    config = json.dumps({"agents": {"campaign-worker": {"steps": 5}}})
    record = ensure_service(project, "opencode", str(tmp_path / "db"),
                            config_content=config,
                            required_agents=["campaign-worker"])
    assert seen["env"]["OPENCODE_CONFIG_CONTENT"] == config
    assert seen["stdin"] is service_module.subprocess.DEVNULL
    assert seen["agents"] == ["campaign-worker"]
    assert record["running"] is True
    assert "OPENCODE_CONFIG_CONTENT" not in json.dumps(read_service(project))


def test_service_that_never_lists_agents_is_stopped(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    stopped = []
    monkeypatch.setattr(service_module.subprocess, "Popen",
                        lambda argv, **kwargs: _FakeProcess())
    monkeypatch.setattr(service_module, "_healthy", lambda port, password: True)
    monkeypatch.setattr(service_module, "_wait_for_agents",
                        lambda *a, **k: False)
    monkeypatch.setattr(service_module, "redact_file", lambda path: None)
    monkeypatch.setattr(service_module, "_stop_process",
                        lambda record: stopped.append(record["pid"]))
    try:
        ensure_service(project, "opencode", str(tmp_path / "db"),
                       required_agents=["campaign-worker"], timeout_seconds=1)
    except ServiceError as exc:
        assert "campaign-worker" in str(exc)
    else:
        raise AssertionError("expected a service without the agents to be rejected")
    assert stopped == [424242]
    assert read_service(project) is None


def test_running_service_must_match_configuration(tmp_path, monkeypatch):
    project = tmp_path / "project"
    path = project / ".magnolia" / "campaign-service.json"
    path.parent.mkdir(parents=True)
    db = str(tmp_path / "same.db")
    path.write_text(json.dumps({
        "project": "project", "port": 4112, "db": db, "pid": 1234, "pgid": 1234,
        "password": "secret", "started_at": 0, "log": "log",
        "config_sha256": service_module._config_digest('{"agents":{}}'),
    }))
    monkeypatch.setattr("campaign_runner.service.service_status",
                        lambda project_dir: {"running": True})
    config = json.dumps({"agents": {"campaign-worker": {"steps": 5}}})
    try:
        ensure_service(project, "opencode", db, config_content=config)
    except ServiceError as exc:
        assert "different configuration" in str(exc)
    else:
        raise AssertionError("expected a configuration mismatch to be rejected")
    # The same configuration reuses the running service.
    assert ensure_service(project, "opencode", db,
                          config_content='{"agents":{}}')["running"] is True


def test_merge_agent_steps_keeps_other_keys():
    merged = json.loads(service_module.merge_agent_steps(
        json.dumps({"model": "m", "agents": {"campaign-worker": {"x": 1}}}),
        {"campaign-worker": 5, "campaign-reviewer": 8}))
    assert merged["model"] == "m"
    assert merged["agents"]["campaign-worker"] == {"x": 1, "steps": 5}
    assert merged["agents"]["campaign-reviewer"] == {"steps": 8}
    assert json.loads(service_module.merge_agent_steps(None, {"a": 2})) == {
        "agents": {"a": {"steps": 2}}}
    assert json.loads(service_module.merge_agent_steps("not json", {"a": 2})) == {
        "agents": {"a": {"steps": 2}}}


def test_service_required_agents_and_startup_steps(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from campaign_runner.cli import _service_settings

    project = tmp_path / "project"
    project.mkdir()
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return _FakeProcess()

    def fake_wait_for_agents(port, password, names, timeout_seconds, interval=0.5):
        seen["required_agents"] = list(names)
        seen["timeout_seconds"] = timeout_seconds
        return True

    protocol = SimpleNamespace(
        worker=SimpleNamespace(max_steps=30),
        reviewer=SimpleNamespace(enabled=True, max_steps=8),
    )
    monkeypatch.setattr(service_module.os, "environ", {"BASE_ENV": "ignored"})
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service_module, "_choose_port", lambda: 4137)
    monkeypatch.setattr(service_module, "_healthy", lambda *args: True)
    monkeypatch.setattr(service_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        service_module,
        "_wait_for_agents",
        fake_wait_for_agents,
    )
    monkeypatch.setattr(service_module, "redact_file", lambda path: None)
    config, required_agents = _service_settings(protocol)
    ensure_service(
        project,
        str(tmp_path / "opencode"),
        str(tmp_path / "opencode.db"),
        config_content=config,
        required_agents=required_agents,
    )
    snapshot = json.loads(
        (ROOT / "tests" / "snapshots" / "opencode" / "service.json").read_text()
    )
    observed = {
        "argv": [item.replace(str(tmp_path), "@@TMP@@") for item in seen["argv"]],
        "environment_keys": sorted(seen["env"]),
        "required_agents": seen["required_agents"],
        "startup_steps": {
            name: value["steps"]
            for name, value in json.loads(config)["agents"].items()
        },
        "timeout_seconds": seen["timeout_seconds"],
        "agent_file_bytes": {
            name: (ROOT / ".opencode" / "agents" / name).read_bytes().decode("utf-8")
            for name in ("campaign-worker.md", "campaign-reviewer.md")
        },
    }
    assert observed == snapshot
