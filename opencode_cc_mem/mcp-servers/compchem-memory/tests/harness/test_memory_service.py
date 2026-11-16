from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from campaign_runner.executors.registry import ExecutorRefused
from compchem_memory.harness import memory_service


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


def _context(project: Path, *, key: str = "provider-secret") -> SimpleNamespace:
    return SimpleNamespace(
        project_dir=project,
        python="/test/python",
        setup={
            "memory_provider": "openai",
            "memory_model": "test-model",
            "memory_service_timeout": 0.01,
        },
        environment={"OPENAI_API_KEY": key},
    )


def _stub_runtime(monkeypatch, identities: dict[int, str] | None = None):
    identities = identities or {4101: "ps:4101:start"}
    started: list[tuple[list[str], dict[str, str]]] = []
    stopped: list[tuple[int, str]] = []

    def start(argv, cwd, environment, log_path):
        started.append((list(argv), dict(environment)))
        return FakeProcess(4101)

    monkeypatch.setattr(memory_service, "_start_process", start)
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43101)
    monkeypatch.setattr(
        memory_service, "_process_identity", lambda pid: identities.get(pid)
    )
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token: {
            "project_digest": memory_service.project_digest(Path(url.split("#")[-1]))
            if "#" in url
            else None,
            "process_identity": identities.get(4101),
        },
    )
    monkeypatch.setattr(
        memory_service,
        "_terminate_owned",
        lambda pid, identity: stopped.append((pid, identity)) or True,
    )
    return started, stopped


def _healthy_for(
    monkeypatch,
    project: Path,
    started: list[tuple[list[str], dict[str, str]]],
    identity: str = "ps:4101:start",
) -> None:
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token: {
            "project_digest": memory_service.project_digest(project),
            "process_identity": identity,
            "service_identity": started[-1][1]["MAGNOLIA_MEMORY_SERVICE_IDENTITY"],
        },
    )


def test_memory_service_reuse_requires_identity_and_readiness(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)

    first = memory_service.ensure_memory_service(_context(project))
    second = memory_service.ensure_memory_service(_context(project))

    assert first.url == second.url
    assert first.process_identity == second.process_identity
    assert len(started) == 1
    assert memory_service.release_memory_service(first) is False
    assert memory_service.release_memory_service(second) is True
    assert stopped == [(4101, "ps:4101:start")]


def test_memory_service_wrong_port_or_stale_pid_not_reused(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    started, stopped = _stub_runtime(
        monkeypatch, {4101: "ps:4101:new", 9999: "ps:9999:unrelated"}
    )
    state = memory_service.private_state_dir(project)
    state.mkdir(parents=True)
    token_path = state / "bearer-token"
    token_path.write_text("service-token", encoding="utf-8")
    os.chmod(token_path, 0o600)
    (state / "service.json").write_text(
        json.dumps(
            {
                "pid": 9999,
                "port": 49999,
                "project_digest": memory_service.project_digest(project),
                "process_identity": "ps:9999:stale",
                "service_identity": "old-service",
                "token_file": str(token_path),
                "holders": [],
            }
        ),
        encoding="utf-8",
    )
    _healthy_for(monkeypatch, project, started, "ps:4101:new")

    handle = memory_service.ensure_memory_service(_context(project))

    assert len(started) == 1
    assert handle.process_identity == "ps:4101:new"
    assert (9999, "ps:9999:stale") not in stopped


def test_missing_key_or_unavailable_service_refuses(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    context = _context(project, key="")

    with pytest.raises(ExecutorRefused, match="provider API key"):
        memory_service.ensure_memory_service(context)

    monkeypatch.setattr(memory_service, "_start_process", lambda *args: FakeProcess(4101))
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43101)
    monkeypatch.setattr(memory_service, "_process_identity", lambda pid: "ps:4101:start")
    monkeypatch.setattr(memory_service, "_health", lambda url, token: None)
    with pytest.raises(ExecutorRefused, match="did not become ready"):
        memory_service.ensure_memory_service(_context(project))


def test_memory_service_keys_never_emitted(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    provider_key = "OPENAI-PROVIDER-KEY-SENTINEL"
    started, _ = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)

    handle = memory_service.ensure_memory_service(_context(project, key=provider_key))
    state = memory_service.private_state_dir(project)
    emitted = "\n".join(
        [
            repr(handle),
            " ".join(started[0][0]),
            (state / "service.json").read_text(encoding="utf-8"),
            (state / "bearer-token").read_text(encoding="utf-8"),
            (state / "service.log").read_text(encoding="utf-8"),
        ]
    )

    assert provider_key not in emitted
    assert provider_key == started[0][1]["OPENAI_API_KEY"]
    assert (state / "bearer-token").stat().st_mode & 0o777 == 0o600


def test_memory_service_cleanup_preserves_other_project(tmp_path, monkeypatch):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    next_pid = iter((4101, 4102))
    identities = {4101: "ps:4101:start", 4102: "ps:4102:start"}
    stopped: list[tuple[int, str]] = []
    service_identities: dict[int, str] = {}

    def start(argv, cwd, environment, log_path):
        pid = next(next_pid)
        service_identities[pid] = environment["MAGNOLIA_MEMORY_SERVICE_IDENTITY"]
        return FakeProcess(pid)

    monkeypatch.setattr(
        memory_service,
        "_start_process",
        start,
    )
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43101)
    monkeypatch.setattr(memory_service, "_process_identity", identities.get)
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token: {
            "project_digest": (
                memory_service.project_digest(project_a)
                if "43101" in url
                else memory_service.project_digest(project_b)
            ),
            "process_identity": identities[4101],
        },
    )
    ports = iter((43101, 43102))
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: next(ports))

    def health(url, token):
        if "43101" in url:
            return {
                "project_digest": memory_service.project_digest(project_a),
                "process_identity": identities[4101],
                "service_identity": service_identities[4101],
            }
        return {
            "project_digest": memory_service.project_digest(project_b),
            "process_identity": identities[4102],
            "service_identity": service_identities[4102],
        }

    monkeypatch.setattr(memory_service, "_health", health)
    monkeypatch.setattr(
        memory_service,
        "_terminate_owned",
        lambda pid, identity: stopped.append((pid, identity)) or True,
    )

    handle_a = memory_service.ensure_memory_service(_context(project_a))
    handle_b = memory_service.ensure_memory_service(_context(project_b))
    memory_service.release_memory_service(handle_a)

    assert stopped == [(4101, "ps:4101:start")]
    assert (memory_service.private_state_dir(project_b) / "service.json").exists()
    assert memory_service.release_memory_service(handle_b) is True
