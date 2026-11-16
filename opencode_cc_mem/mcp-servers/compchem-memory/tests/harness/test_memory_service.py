from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from campaign_runner.executors.registry import ExecutorRefused
from compchem_memory.harness import memory_service
from compchem_memory.harness import __main__ as harness_main


@pytest.fixture(autouse=True)
def _private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


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


def test_private_state_is_user_private_and_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    state = memory_service.private_state_dir(project)

    assert state == (
        Path.home()
        / ".magnolia"
        / "services"
        / memory_service.project_digest(project)
    )
    assert not state.is_relative_to(project)


def test_provider_must_be_explicit_and_other_key_names_do_not_count():
    context = SimpleNamespace(setup={})

    with pytest.raises(ExecutorRefused, match="configure.*memory_provider"):
        memory_service._require_provider_key(
            context,
            {"MISTRAL_API_KEY": "client-credential-like"},
        )


def test_service_environment_is_provider_specific_allowlist():
    context = SimpleNamespace(
        setup={"memory_provider": "openai", "memory_model": "model"},
        environment={
            "PATH": "/bin",
            "HOME": "/home/member",
            "PYTHONPATH": "/install/src",
            "OPENAI_API_KEY": "selected",
            "DEEPSEEK_API_KEY": "other-provider",
            "ANTHROPIC_AUTH_TOKEN": "client-subscription",
            "UNRELATED_SECRET": "nope",
        },
    )

    environment = memory_service._server_environment(context)

    assert environment["OPENAI_API_KEY"] == "selected"
    assert environment["PATH"] == "/bin"
    assert environment["PYTHONPATH"] == "/install/src"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert "UNRELATED_SECRET" not in environment


def test_service_environment_preserves_server_runtime_knobs_and_proxy_variants():
    context = SimpleNamespace(
        setup={"memory_provider": "openai", "memory_model": "model"},
        environment={
            "OPENAI_API_KEY": "selected",
            "DEEPSEEK_API_KEY": "other-provider",
            "MAGNOLIA_RULES_DIR": "/rules",
            "MAGNOLIA_DISTILL_INTERVAL_MIN": "17",
            "MAGNOLIA_LLM_PROVIDER": "openai",
            "MAGNOLIA_LLM_MODEL": "legacy-model",
            "MAGNOLIA_CONSOLIDATION_FLOOR": "0.7",
            "MAGNOLIA_OPENROUTER_STICKY": "0",
            "MAGNOLIA_CRITIC_PROVIDER": "openrouter",
            "MAGNOLIA_CRITIC_API_KEY": "different-secret",
            "http_proxy": "http://proxy.test",
            "ALL_PROXY": "socks5://proxy.test",
        },
    )

    environment = memory_service._server_environment(context)

    assert environment["MAGNOLIA_RULES_DIR"] == "/rules"
    assert environment["MAGNOLIA_DISTILL_INTERVAL_MIN"] == "17"
    assert environment["MAGNOLIA_LLM_PROVIDER"] == "openai"
    assert environment["MAGNOLIA_LLM_MODEL"] == "legacy-model"
    assert environment["MAGNOLIA_CONSOLIDATION_FLOOR"] == "0.7"
    assert environment["MAGNOLIA_OPENROUTER_STICKY"] == "0"
    assert environment["MAGNOLIA_CRITIC_PROVIDER"] == "openrouter"
    assert environment["http_proxy"] == "http://proxy.test"
    assert environment["ALL_PROXY"] == "socks5://proxy.test"
    assert "MAGNOLIA_CRITIC_API_KEY" not in environment
    assert "DEEPSEEK_API_KEY" not in environment


def test_preexisting_magnolia_directory_mode_is_unchanged(tmp_path):
    root = Path.home() / ".magnolia"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()

    state = memory_service.private_state_dir(project)
    memory_service._ensure_private_state_dir(state)

    assert root.stat().st_mode & 0o777 == 0o755
    assert (root / "services").stat().st_mode & 0o777 == 0o700
    assert state.stat().st_mode & 0o777 == 0o700


def test_harness_launch_cli_requires_holder_pid():
    with pytest.raises(ValueError, match="holder-pid"):
        harness_main._parse(
            [
                "launch",
                "--harness",
                "vibe",
                "--root",
                "/install",
                "--project",
                "project",
                "--python",
                "/python",
                "--tools-port",
                "8123",
            ]
        )


def _stub_runtime(monkeypatch, identities: dict[int, str] | None = None):
    identities = identities or {4101: "ps:4101:start"}
    identities.setdefault(os.getpid(), f"ps:{os.getpid()}:test-launcher")
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
        "_process_command",
        lambda pid: ("/test/python", "-m", "compchem_memory.memory_http")
        if pid in identities
        else None,
    )
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token, timeout=2.0: {
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
        lambda url, token, timeout=2.0: {
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


def test_unverifiable_holder_is_refused_before_state_or_start(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    context = _context(project)
    context.holder_pid = 5999
    starts = []
    monkeypatch.setattr(memory_service, "_process_identity", lambda pid: None)
    monkeypatch.setattr(
        memory_service,
        "_start_process",
        lambda *args: starts.append(args) or FakeProcess(4101),
    )

    with pytest.raises(ExecutorRefused, match="holder.*could not be verified"):
        memory_service.ensure_memory_service(context)

    assert starts == []
    assert not memory_service.private_state_dir(project).exists()


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


def test_forged_identity_correct_record_never_signals_unrelated_process(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    state.mkdir(parents=True)
    token_path = state / "bearer-token"
    token_path.write_text("service-token\n", encoding="utf-8")
    token_path.chmod(0o600)
    victim_pid = 9999
    victim_identity = "ps:9999:victim-start"
    record = {
        "pid": victim_pid,
        "port": 43101,
        "project_digest": "0" * 64,
        "process_identity": victim_identity,
        "service_identity": "forged",
        "token_file": str(token_path),
        "command": ["/bin/sleep", "600"],
        "holders": [],
    }
    (state / "service.json").write_text(json.dumps(record), encoding="utf-8")
    (state / "service.json").chmod(0o600)
    stopped = []
    monkeypatch.setattr(
        memory_service,
        "_process_identity",
        lambda pid: victim_identity if pid == victim_pid else "ps:4101:new",
    )
    monkeypatch.setattr(
        memory_service,
        "_process_command",
        lambda pid: ("/bin/sleep", "600") if pid == victim_pid else (
            "/test/python", "-m", "compchem_memory.memory_http"
        ),
        raising=False,
    )
    monkeypatch.setattr(
        memory_service,
        "_terminate_owned",
        lambda pid, identity, *args: stopped.append((pid, identity)) or True,
    )
    monkeypatch.setattr(memory_service, "_start_process", lambda *args: FakeProcess(4101))
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43102)
    service_identity = []

    def health(url, token, timeout=2.0):
        return {
            "project_digest": memory_service.project_digest(project),
            "process_identity": "ps:4101:new",
            "service_identity": service_identity[-1],
        }

    def start(argv, cwd, environment, log_path):
        service_identity.append(environment["MAGNOLIA_MEMORY_SERVICE_IDENTITY"])
        return FakeProcess(4101)

    monkeypatch.setattr(memory_service, "_start_process", start)
    monkeypatch.setattr(memory_service, "_health", health)

    memory_service.ensure_memory_service(_context(project))

    assert (victim_pid, victim_identity) not in stopped


def test_forged_record_naming_other_project_service_is_not_signalled(
    tmp_path, monkeypatch
):
    project_c = tmp_path / "project-c"
    project_d = tmp_path / "project-d"
    project_c.mkdir()
    project_d.mkdir()
    state = memory_service.private_state_dir(project_c)
    memory_service._ensure_private_state_dir(state)
    token_path = state / "bearer-token"
    token_path.write_text("service-token\n", encoding="utf-8")
    token_path.chmod(0o600)
    service_pid = 4200
    replacement_pid = 4300
    identities = {
        os.getpid(): f"ps:{os.getpid()}:holder",
        service_pid: "ps:4200:project-d-service",
        replacement_pid: "ps:4300:project-c-service",
    }
    forged = {
        "pid": service_pid,
        "port": 44200,
        "project_digest": memory_service.project_digest(project_c),
        "project_dir": str(project_c.resolve()),
        "process_identity": identities[service_pid],
        "service_identity": "project-d-identity",
        "token_file": str(token_path),
        "command": ["/test/python", "-m", memory_service.SERVICE_MODULE],
        "holders": [],
    }
    memory_service._write_record(state, forged)
    stopped = []
    started_environment = []
    monkeypatch.setattr(memory_service, "_process_identity", identities.get)
    monkeypatch.setattr(
        memory_service,
        "_process_command",
        lambda pid: tuple(forged["command"])
        if pid == service_pid
        else (
            "/test/python",
            "-m",
            memory_service.SERVICE_MODULE,
            "--project-dir",
            str(project_c.resolve()),
        ),
    )
    monkeypatch.setattr(
        memory_service,
        "_terminate_owned",
        lambda pid, identity: stopped.append((pid, identity)) or True,
    )
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 44300)

    def start(argv, cwd, environment, log_path):
        started_environment.append(environment)
        return FakeProcess(replacement_pid)

    monkeypatch.setattr(memory_service, "_start_process", start)
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token, timeout=2.0: (
            {
                "project_digest": memory_service.project_digest(project_c),
                "process_identity": identities[replacement_pid],
                "service_identity": started_environment[-1][
                    "MAGNOLIA_MEMORY_SERVICE_IDENTITY"
                ],
            }
            if "44300" in url
            else {
                "project_digest": memory_service.project_digest(project_d),
                "process_identity": identities[service_pid],
                "service_identity": "project-d-identity",
            }
        ),
    )

    memory_service.ensure_memory_service(_context(project_c))

    assert (service_pid, identities[service_pid]) not in stopped


def test_signal_safety_accepts_exact_project_command_without_health_token(
    tmp_path, monkeypatch
):
    project = (tmp_path / "project").resolve()
    project.mkdir()
    pid = 4250
    identity = "ps:4250:service"
    command = [
        "/test/python",
        "-m",
        memory_service.SERVICE_MODULE,
        "--project-dir",
        str(project),
    ]
    record = {
        "pid": pid,
        "port": 44250,
        "project_digest": memory_service.project_digest(project),
        "project_dir": str(project),
        "process_identity": identity,
        "service_identity": "service-identity",
        "token_file": str(tmp_path / "missing-token"),
        "command": command,
    }
    monkeypatch.setattr(memory_service, "_process_identity", lambda value: identity)
    monkeypatch.setattr(memory_service, "_process_command", lambda value: tuple(command))
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda *args: pytest.fail("health must not run without a private token"),
    )

    assert memory_service._service_signal_safe(record) is True


def test_real_forged_record_does_not_kill_unrelated_process(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    memory_service._ensure_private_state_dir(state)
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = memory_service._process_identity(victim.pid)
        if not identity:
            pytest.skip("process start identity is unavailable on this platform")
        record = {
            "pid": victim.pid,
            "project_digest": memory_service.project_digest(project),
            "process_identity": identity,
            "command": [sys.executable, "-m", memory_service.SERVICE_MODULE],
        }

        memory_service._discard_record(
            record,
            state,
            memory_service.project_digest(project),
        )

        assert victim.poll() is None
    finally:
        victim.terminate()
        victim.wait(timeout=5)


def test_health_reuse_uses_two_second_timeout_and_retries_once(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    started, _ = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    first = memory_service.ensure_memory_service(_context(project))
    expected = {
        "project_digest": first.project_digest,
        "process_identity": first.process_identity,
        "service_identity": first.service_identity,
    }
    calls = []

    def health(url, token, timeout=0):
        calls.append(timeout)
        return None if len(calls) == 1 else expected

    monkeypatch.setattr(memory_service, "_health", health)

    second = memory_service.ensure_memory_service(_context(project))

    assert second.process_identity == first.process_identity
    assert calls == [2.0, 2.0]


def test_holders_are_the_long_lived_launchers(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    identities = {
        4101: "ps:4101:service",
        5101: "ps:5101:launcher-one",
        5102: "ps:5102:launcher-two",
    }
    started, stopped = _stub_runtime(monkeypatch, identities)
    _healthy_for(monkeypatch, project, started, identities[4101])
    first_context = _context(project)
    first_context.holder_pid = 5101
    second_context = _context(project)
    second_context.holder_pid = 5102

    first = memory_service.ensure_memory_service(first_context)
    second = memory_service.ensure_memory_service(second_context)
    record = json.loads(
        (memory_service.private_state_dir(project) / "service.json").read_text()
    )

    assert {holder["pid"] for holder in record["holders"]} == {5101, 5102}
    assert memory_service.release_memory_service(first) is False
    assert stopped == []
    assert memory_service.release_memory_service(second) is True
    assert stopped == [(4101, identities[4101])]


def test_next_acquire_reaps_service_when_all_holders_are_dead(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    memory_service.ensure_memory_service(_context(project))
    state = memory_service.private_state_dir(project)
    record = json.loads((state / "service.json").read_text())
    record["holders"] = [
        {
            "id": "dead-holder",
            "pid": 5999,
            "process_identity": "ps:5999:dead",
        }
    ]
    memory_service._write_record(state, record)

    replacement = memory_service.ensure_memory_service(_context(project))

    assert len(started) == 2
    assert stopped == [(4101, "ps:4101:start")]
    assert replacement.service_identity != record["service_identity"]


def test_missing_key_or_unavailable_service_refuses(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    context = _context(project, key="")
    monkeypatch.setattr(
        memory_service,
        "_process_identity",
        lambda pid: f"ps:{pid}:holder" if pid == os.getpid() else "ps:4101:start",
    )

    with pytest.raises(ExecutorRefused, match="provider API key"):
        memory_service.ensure_memory_service(context)

    monkeypatch.setattr(memory_service, "_start_process", lambda *args: FakeProcess(4101))
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43101)
    monkeypatch.setattr(memory_service, "_health", lambda url, token, timeout=2.0: None)
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
    identities[os.getpid()] = f"ps:{os.getpid()}:test-launcher"
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
        "_process_command",
        lambda pid: ("/test/python", "-m", "compchem_memory.memory_http")
        if pid in {4101, 4102}
        else None,
    )
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token, timeout=2.0: {
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

    def health(url, token, timeout=2.0):
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
