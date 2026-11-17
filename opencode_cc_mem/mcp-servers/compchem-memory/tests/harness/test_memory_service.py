from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from campaign_runner.executors.registry import ExecutorRefused
from campaign_runner import governor as governor_module
from compchem_memory.harness import memory_service
from compchem_memory.harness import __main__ as harness_main


@pytest.fixture(autouse=True)
def _private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", "")
    original_state_dir = memory_service.private_state_dir

    def keyed_state_dir(project, tool_allowlist=None, environment=None):
        tools = _context(Path(project)).tool_allowlist if tool_allowlist is None else tool_allowlist
        return original_state_dir(Path(project), tools, environment=environment)

    monkeypatch.setattr(memory_service, "private_state_dir", keyed_state_dir)


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
        holder_pid=os.getpid(),
        tool_allowlist=(
            "mcp:compchem-memory.memory_get_context",
            "mcp:compchem-memory.memory_search",
            "mcp:compchem-memory.memory_get_run_history",
        ),
    )


def _service_argv(
    project: Path, state: Path | None = None, *, python: str = "/test/python"
) -> tuple[str, ...]:
    argv = (
        python,
        "-I",
        memory_service.repository_runner_path(),
        "--memory-http",
        "--project-dir",
        str(project.resolve()),
    )
    return (*argv, "--state-dir", str(state)) if state is not None else argv


def test_private_state_is_user_private_and_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    state = memory_service.private_state_dir(project)

    assert state == (
        Path.home()
        / ".magnolia"
        / "services"
        / memory_service.project_digest(project)
        / memory_service.tool_allowlist_digest(_context(project).tool_allowlist)
    )
    assert not state.is_relative_to(project)


@pytest.mark.parametrize(
    ("xdg_state_home", "uses_xdg"),
    (("absolute", True), ("", False), ("relative/state", False)),
)
def test_private_state_root_follows_absolute_xdg_state_home(
    tmp_path, xdg_state_home, uses_xdg
):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "launcher-home"
    state_home = tmp_path / "launcher-state"
    environment = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home) if xdg_state_home == "absolute" else xdg_state_home,
    }

    state = memory_service.private_state_dir(
        project, _context(project).tool_allowlist, environment=environment
    )

    expected_root = (
        state_home / "magnolia" / "services"
        if uses_xdg
        else home / ".magnolia" / "services"
    )
    assert state.parent.parent == expected_root.resolve()


def test_provider_must_be_explicit_and_other_key_names_do_not_count():
    context = SimpleNamespace(setup={})

    with pytest.raises(ExecutorRefused, match="configure.*memory_provider"):
        memory_service._require_provider_key(
            context,
            {"MISTRAL_API_KEY": "client-credential-like"},
        )


def test_service_acquisition_refuses_missing_tool_allowlist(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    context = _context(project)
    del context.tool_allowlist

    with pytest.raises(ExecutorRefused, match="tool allowlist is required"):
        memory_service.ensure_memory_service(context)


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


@pytest.mark.parametrize(
    "name",
    ("VENDOR_SPECIFIC_API_KEY", "vendor_specific_api_key", "Magnolia_Custom_Api_Key"),
)
def test_provider_key_name_matches_every_case_insensitive_suffix(name):
    assert memory_service.is_provider_api_key_name(name)


def test_managed_service_bytecode_stays_in_private_state(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "imported_by_service.py").write_text("VALUE = 42\n", encoding="utf-8")
    marker = tmp_path / "imported"
    probe = project / "service_probe.py"
    imported = project / "imported_by_service.py"
    probe.write_text(
        "import importlib.util, os, pathlib, sys, time\n"
        "sys.pycache_prefix = os.environ.get('PYTHONPYCACHEPREFIX')\n"
        f"spec = importlib.util.spec_from_file_location('imported_by_service', {str(imported)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(module.VALUE))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    started = {}

    def start(command, cwd, environment, log_path):
        started["command"] = list(command)
        started["environment"] = dict(environment)
        log = log_path.open("ab")
        process = subprocess.Popen(
            [sys.executable, "-I", str(probe)],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        log.close()
        started["process"] = process
        return process

    def process_identity(pid):
        return f"ps:{pid}:test"

    def wait_ready(process, url, token, timeout):
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.read_text() == "42"
        return {
            "project_digest": memory_service.project_digest(project),
            "process_identity": process_identity(process.pid),
            "service_identity": started["environment"][
                "MAGNOLIA_MEMORY_SERVICE_IDENTITY"
            ],
        }

    def terminate(record):
        process = started["process"]
        process.kill()
        process.wait(timeout=5)
        return True

    monkeypatch.setattr(memory_service, "_start_process", start)
    monkeypatch.setattr(memory_service, "_process_identity", process_identity)
    monkeypatch.setattr(
        memory_service, "_process_command", lambda pid: tuple(started["command"])
    )
    monkeypatch.setattr(memory_service, "_listener_owned_by_pid", lambda pid, port: True)
    monkeypatch.setattr(memory_service, "_wait_ready", wait_ready)
    monkeypatch.setattr(memory_service, "_terminate_owned", terminate)
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43101)

    handle = memory_service.ensure_memory_service(_context(project))
    state = Path(handle.state_dir)
    try:
        assert started["environment"]["PYTHONPYCACHEPREFIX"] == str(
            state / "pycache"
        )
        assert not list(project.rglob("__pycache__"))
        assert list((state / "pycache").rglob("*.pyc"))
    finally:
        stopped = memory_service.release_memory_service(handle)
    assert stopped is True


def test_service_environment_includes_explicit_critic_and_direct_fallback_routes():
    context = SimpleNamespace(
        setup={
            "memory_provider": "openrouter",
            "memory_model": "deepseek/model",
            "critic_provider": "openrouter",
            "critic_model": "deepseek/judge",
            "direct_fallback_provider": "deepseek",
            "direct_fallback_model": "deepseek-flash",
        },
        environment={
            "OPENROUTER_API_KEY": "router",
            "DEEPSEEK_API_KEY": "fallback",
            "ANTHROPIC_API_KEY": "unrelated",
        },
    )

    environment = memory_service._server_environment(context)

    assert environment["MAGNOLIA_CRITIC_PROVIDER"] == "openrouter"
    assert environment["MAGNOLIA_CRITIC_MODEL"] == "deepseek/judge"
    assert environment["MAGNOLIA_DIRECT_FALLBACK_PROVIDER"] == "deepseek"
    assert environment["MAGNOLIA_DIRECT_FALLBACK_MODEL"] == "deepseek-flash"
    assert environment["OPENROUTER_API_KEY"] == "router"
    assert environment["DEEPSEEK_API_KEY"] == "fallback"
    assert "ANTHROPIC_API_KEY" not in environment


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
            "MAGNOLIA_MEMORY_SERVICE_WATCHDOG_S": "0.25",
            "MAGNOLIA_MEMORY_SERVICE_GRACE_S": "3",
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
    assert environment["MAGNOLIA_MEMORY_SERVICE_WATCHDOG_S"] == "0.25"
    assert environment["MAGNOLIA_MEMORY_SERVICE_GRACE_S"] == "3"
    assert environment["http_proxy"] == "http://proxy.test"
    assert environment["ALL_PROXY"] == "socks5://proxy.test"
    assert "MAGNOLIA_CRITIC_API_KEY" not in environment
    assert "DEEPSEEK_API_KEY" not in environment


def test_service_and_governor_share_linux_process_identity_implementation(
    tmp_path, monkeypatch
):
    pid = 4242
    fields = [b"S", b"1", b"4242", *([b"1"] * 16), b"987654"]
    proc_body = b"4242 (worker) " + b" ".join(fields) + b"\n"
    parent_token = governor_module._verified_process_start_token
    monkeypatch.setattr(
        parent_token.__globals__["Path"], "read_bytes", lambda *_a, **_k: proc_body
    )
    parent_value = parent_token(pid)
    runner = Path(memory_service.repository_runner_path())
    probe = tmp_path / "identity_probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import importlib.util
            import json

            runner_path = {str(runner)!r}
            spec = importlib.util.spec_from_file_location("magnolia_runner", runner_path)
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
            runner._load_repository_package(runner._repository_source())
            from compchem_memory.harness import memory_service

            token = memory_service._verified_process_start_token
            proc_body = {proc_body!r}
            token.__globals__["Path"].read_bytes = lambda *_a, **_k: proc_body
            print(json.dumps({{"module": token.__module__, "value": token({pid})}}))
            """
        ),
        encoding="utf-8",
    )

    child = subprocess.run(
        [sys.executable, "-B", "-I", str(probe)],
        check=True,
        capture_output=True,
        text=True,
    )
    child_result = json.loads(child.stdout)

    assert parent_token.__module__ == "compchem_memory.process_identity"
    assert child_result["module"] == parent_token.__module__
    assert child_result["value"] == parent_value == "proc:4242:987654"


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


@pytest.mark.parametrize("home_kind", ["explicit_symlink", "macos_tmp_alias"])
def test_symlinked_home_acquire_reuse_release_uses_canonical_state(
    tmp_path, monkeypatch, home_kind
):
    real_home = tmp_path / f"real-home-{home_kind}"
    real_home.mkdir()
    if home_kind == "explicit_symlink":
        home = tmp_path / "linked-home"
        home.symlink_to(real_home, target_is_directory=True)
    else:
        private_tmp = Path("/private/tmp")
        tmp_alias = Path("/tmp")
        try:
            relative = real_home.relative_to(private_tmp)
        except ValueError:
            pytest.skip("pytest temp directory is not under /private/tmp")
        if tmp_alias.resolve() != private_tmp:
            pytest.skip("/tmp is not the macOS /private/tmp alias on this platform")
        home = tmp_alias / relative
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / f"project-{home_kind}"
    project.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)

    first = memory_service.ensure_memory_service(_context(project))
    second = memory_service.ensure_memory_service(_context(project))

    expected_state = (
        real_home
        / ".magnolia"
        / "services"
        / memory_service.project_digest(project)
        / memory_service.tool_allowlist_digest(_context(project).tool_allowlist)
    ).resolve()
    state_flag = started[0][0].index("--state-dir")
    assert Path(started[0][0][state_flag + 1]) == expected_state
    record = json.loads((expected_state / "service.json").read_text())
    assert Path(record["state_dir"]) == expected_state
    assert Path(record["token_file"]).parent == expected_state
    assert memory_service.release_memory_service(first) is False
    assert memory_service.release_memory_service(second) is True
    assert stopped == [(4101, "ps:4101:start")]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (1, "", "", False),
        (-9, "", "", None),
        (1, "", "ps: sysctl: Cannot allocate memory\n", None),
        (2, "", "ps: internal error\n", None),
    ],
)
def test_process_exists_treats_only_clear_ps_not_found_as_dead(
    monkeypatch, returncode, stdout, stderr, expected
):
    monkeypatch.setattr(memory_service.sys, "platform", "darwin")
    monkeypatch.setattr(
        memory_service.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode, stdout, stderr
        ),
    )

    assert memory_service._process_exists(987654) is expected


def test_process_exists_uses_process_lookup_error_as_linux_not_found(monkeypatch):
    monkeypatch.setattr(memory_service.sys, "platform", "linux")

    def missing(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(memory_service.os, "kill", missing)

    assert memory_service._process_exists(os.getpid()) is False


def test_write_record_removes_temporary_file_after_failure(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    memory_service._ensure_private_state_dir(state)
    monkeypatch.setattr(
        memory_service.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError(28, "disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        memory_service._write_record(state, {"pid": 1})

    assert list(state.glob(".service.json.*.tmp")) == []


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
    commands: dict[int, tuple[str, ...]] = {}

    def start(argv, cwd, environment, log_path):
        started.append((list(argv), dict(environment)))
        commands[4101] = tuple(argv)
        return FakeProcess(4101)

    monkeypatch.setattr(memory_service, "_start_process", start)
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43101)
    monkeypatch.setattr(
        memory_service, "_process_identity", lambda pid: identities.get(pid)
    )
    monkeypatch.setattr(memory_service, "_listener_owned_by_pid", lambda pid, port: True)
    monkeypatch.setattr(
        memory_service,
        "_process_command",
        commands.get,
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
        lambda record: stopped.append((record["pid"], record["process_identity"])) or True,
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
    assert started[0][1]["MAGNOLIA_MEMORY_SERVICE_ACQUIRER_PID"] == str(os.getpid())
    assert started[0][1]["MAGNOLIA_MEMORY_SERVICE_ACQUIRER_IDENTITY"] == (
        f"ps:{os.getpid()}:test-launcher"
    )
    state = memory_service.private_state_dir(project)
    state_flag = started[0][0].index("--state-dir")
    assert Path(started[0][0][state_flag + 1]) == state
    assert memory_service.release_memory_service(first) is False
    assert memory_service.release_memory_service(second) is True
    assert stopped == [(4101, "ps:4101:start")]


def test_release_keeps_service_lock_and_state_directories(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    context = _context(project)
    context.environment.update(
        HOME=str(tmp_path / "launcher-home"),
        XDG_STATE_HOME=str(tmp_path / "launcher-state"),
    )
    started, _stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)

    handle = memory_service.ensure_memory_service(context)
    state = Path(handle.state_dir)
    project_state = state.parent
    lock = state / "service.lock"

    assert lock.is_file()

    assert memory_service.release_memory_service(handle) is True
    assert state.is_dir()
    assert project_state.is_dir()
    assert lock.is_file()
    assert not (state / "service.json").exists()
    assert not (state / "bearer-token").exists()


def test_memory_service_does_not_reuse_changed_runtime_route(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    identities = {
        os.getpid(): f"ps:{os.getpid()}:test-launcher",
        4101: "ps:4101:first",
    }
    started, stopped = _stub_runtime(monkeypatch, identities)
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token, timeout=2.0: {
            "project_digest": memory_service.project_digest(project),
            "process_identity": identities[4101],
            "service_identity": started[-1][1]["MAGNOLIA_MEMORY_SERVICE_IDENTITY"],
        },
    )

    first = memory_service.ensure_memory_service(_context(project))
    changed = _context(project)
    changed.setup["memory_model"] = "changed-model"

    with pytest.raises(ExecutorRefused, match="runtime.*live holder"):
        memory_service.ensure_memory_service(changed)

    assert len(started) == 1
    assert stopped == []
    record = json.loads(
        (memory_service.private_state_dir(project) / "service.json").read_text()
    )
    assert len(record["runtime_digest"]) == 64
    assert "provider-secret" not in json.dumps(record)
    assert memory_service.release_memory_service(first) is True
    assert stopped == [(4101, "ps:4101:first")]


def test_runtime_fingerprint_ignores_process_environment_noise():
    route = {
        "PATH": "/first/bin",
        "TMPDIR": "/first/tmp",
        "MAGNOLIA_MEMORY_PROVIDER": "openai",
        "MAGNOLIA_MEMORY_MODEL": "model",
        "OPENAI_API_KEY": "secret",
    }
    noisy = dict(route, PATH="/second/bin", TMPDIR="/second/tmp")

    assert memory_service.service_environment_digest(route) == (
        memory_service.service_environment_digest(noisy)
    )
    assert memory_service.service_environment_digest(route) != (
        memory_service.service_environment_digest(dict(route, OPENAI_API_KEY="rotated"))
    )


def test_pre_fingerprint_service_with_live_holder_is_not_replaced(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    first = memory_service.ensure_memory_service(_context(project))
    state = memory_service.private_state_dir(project)
    record = json.loads((state / "service.json").read_text(encoding="utf-8"))
    record.pop("runtime_digest")
    memory_service._write_record(state, record)

    with pytest.raises(ExecutorRefused, match="runtime.*live holder"):
        memory_service.ensure_memory_service(_context(project))

    assert len(started) == 1
    assert stopped == []
    assert memory_service.release_memory_service(first) is True


def test_dead_service_with_recorded_live_holder_is_replaced(tmp_path, monkeypatch):
    """A holder whose service died holds nothing, so replacement is safe (fix-N m7)."""
    project = tmp_path / "project"
    project.mkdir()
    identities = {
        os.getpid(): f"ps:{os.getpid()}:test-launcher",
        4101: "ps:4101:first",
    }
    started, stopped = _stub_runtime(monkeypatch, identities)
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token, timeout=2.0: {
            "project_digest": memory_service.project_digest(project),
            "process_identity": identities.get(4101),
            "service_identity": started[-1][1]["MAGNOLIA_MEMORY_SERVICE_IDENTITY"],
        },
    )
    first = memory_service.ensure_memory_service(_context(project))
    assert first is not None
    identities.pop(4101)  # the service died; the first holder is still alive
    real_start = memory_service._start_process

    def restart(argv, cwd, environment, log_path):
        identities[4101] = "ps:4101:second"
        return real_start(argv, cwd, environment, log_path)

    monkeypatch.setattr(memory_service, "_start_process", restart)
    changed = _context(project)
    changed.setup["memory_model"] = "changed-model"

    second = memory_service.ensure_memory_service(changed)

    assert second is not None
    assert len(started) == 2
    assert stopped == []


def test_different_allowlists_get_independent_live_services(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    full = tuple(
        f"mcp:compchem-memory.{name}"
        for name in memory_service.resolve_memory_tool_allowlist(
            None, required=False, legacy_stdio=True
        ).server_names
    )
    worker = _context(project).tool_allowlist
    identities = {
        os.getpid(): f"ps:{os.getpid()}:test-launcher",
        4101: "ps:4101:full",
        4102: "ps:4102:worker",
    }
    started = []
    commands = {}
    stopped = []
    ports = iter((43101, 43102))

    def start(argv, cwd, environment, log_path):
        pid = 4101 + len(started)
        started.append((pid, dict(environment)))
        commands[pid] = tuple(argv)
        return FakeProcess(pid)

    monkeypatch.setattr(memory_service, "_start_process", start)
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: next(ports))
    monkeypatch.setattr(memory_service, "_process_identity", identities.get)
    monkeypatch.setattr(memory_service, "_process_command", commands.get)
    monkeypatch.setattr(memory_service, "_listener_owned_by_pid", lambda pid, port: True)
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda url, token, timeout=2.0: {
            "project_digest": memory_service.project_digest(project),
            "process_identity": identities[4101 if ":43101/" in url else 4102],
            "service_identity": started[0 if ":43101/" in url else 1][1][
                "MAGNOLIA_MEMORY_SERVICE_IDENTITY"
            ],
        },
    )
    monkeypatch.setattr(
        memory_service,
        "_terminate_owned",
        lambda record: stopped.append(record["pid"]) or True,
    )

    interactive_handle = memory_service.ensure_memory_service(
        _context(project), tool_allowlist=full
    )
    worker_handle = memory_service.ensure_memory_service(
        _context(project), tool_allowlist=worker
    )

    assert len(started) == 2
    assert interactive_handle.url != worker_handle.url
    assert interactive_handle.token_file.parent != worker_handle.token_file.parent
    assert memory_service.release_memory_service(worker_handle) is True
    assert stopped == [4102]
    assert interactive_handle.token_file.exists()
    assert memory_service.release_memory_service(interactive_handle) is True
    assert stopped == [4102, 4101]


def test_release_preserves_holder_when_identity_probe_is_unknown(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    identities = {
        4101: "ps:4101:start",
        os.getpid(): f"ps:{os.getpid()}:test-launcher",
    }
    started, stopped = _stub_runtime(monkeypatch, identities)
    _healthy_for(monkeypatch, project, started)
    first = memory_service.ensure_memory_service(_context(project))
    second = memory_service.ensure_memory_service(_context(project))
    holder_identity = identities.pop(os.getpid())
    monkeypatch.setattr(memory_service, "_process_exists", lambda pid: True)

    assert memory_service.release_memory_service(first) is False
    record = json.loads(
        (memory_service.private_state_dir(project) / "service.json").read_text()
    )
    assert [holder["id"] for holder in record["holders"]] == [second.holder_id]
    assert stopped == []

    identities[os.getpid()] = holder_identity
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
        "observed_command": ["/bin/sleep", "600"],
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
            *_service_argv(project, state),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        memory_service,
        "_terminate_owned",
        lambda record: stopped.append((record["pid"], record["process_identity"])) or True,
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
        "command": list(_service_argv(project_d)),
        "holders": [],
    }
    forged["observed_command"] = forged["command"]
    memory_service._write_record(state, forged)
    stopped = []
    started_environment = []
    monkeypatch.setattr(memory_service, "_process_identity", identities.get)
    monkeypatch.setattr(
        memory_service,
        "_process_command",
        lambda pid: tuple(forged["command"])
        if pid == service_pid
        else _service_argv(project_c, state),
    )
    monkeypatch.setattr(
        memory_service,
        "_terminate_owned",
        lambda record: stopped.append((record["pid"], record["process_identity"])) or True,
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
                "project_digest": memory_service.project_digest(project_c),
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
    command = list(_service_argv(project))
    record = {
        "pid": pid,
        "port": 44250,
        "project_digest": memory_service.project_digest(project),
        "project_dir": str(project),
        "process_identity": identity,
        "service_identity": "service-identity",
        "token_file": str(tmp_path / "missing-token"),
        "command": command,
        "observed_command": command,
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


def test_forged_health_sleep_record_is_never_signalled_by_ensure_or_release(
    tmp_path, monkeypatch, caplog
):
    project = tmp_path / "project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    memory_service._ensure_private_state_dir(state)
    token_path = state / "bearer-token"
    token_path.write_text("forged-token\n", encoding="utf-8")
    token_path.chmod(0o600)
    victim = subprocess.Popen(
        ["/bin/sleep", "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        victim_identity = f"ps:{victim.pid}:victim"
        holder_identity = f"ps:{os.getpid()}:holder"
        replacement_pid = 4101
        replacement_identity = "ps:4101:replacement"
        service_identities: list[str] = []
        signalled: list[tuple[int, str]] = []
        forged = {
            "pid": victim.pid,
            "port": 44100,
            "project_digest": memory_service.project_digest(project),
            "project_dir": str(project.resolve()),
            "process_identity": victim_identity,
            "service_identity": "forged-service",
            "token_file": str(token_path),
            "command": ["/bin/sleep", "30"],
            "observed_command": ["/bin/sleep", "30"],
            "holders": [],
        }
        memory_service._write_record(state, forged)

        def identity(pid):
            return {
                victim.pid: victim_identity,
                replacement_pid: replacement_identity,
                os.getpid(): holder_identity,
            }.get(pid)

        def start(argv, cwd, environment, log_path):
            service_identities.append(environment["MAGNOLIA_MEMORY_SERVICE_IDENTITY"])
            return FakeProcess(replacement_pid)

        def health(url, token, timeout=2.0):
            if "44100" in url:
                return {
                    "project_digest": memory_service.project_digest(project),
                    "process_identity": victim_identity,
                    "service_identity": "forged-service",
                }
            return {
                "project_digest": memory_service.project_digest(project),
                "process_identity": replacement_identity,
                "service_identity": service_identities[-1],
            }

        monkeypatch.setattr(memory_service, "_process_identity", identity)
        monkeypatch.setattr(
            memory_service,
            "_process_command",
            lambda pid: ("/bin/sleep", "30")
            if pid == victim.pid
            else _service_argv(project, state),
        )
        monkeypatch.setattr(memory_service, "_start_process", start)
        monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 44101)
        monkeypatch.setattr(memory_service, "_health", health)
        monkeypatch.setattr(
            memory_service,
            "_terminate_owned",
            lambda record: signalled.append((record["pid"], record["process_identity"])) or True,
        )

        replacement = memory_service.ensure_memory_service(_context(project))

        assert victim.poll() is None
        assert (victim.pid, victim_identity) not in signalled
        assert "left untouched" in caplog.text
        memory_service._write_record(state, forged)
        forged_handle = memory_service.ServiceHandle(
            url="http://127.0.0.1:44100/mcp",
            project_digest=memory_service.project_digest(project),
            process_identity=victim_identity,
            project_dir=project.resolve(),
            holder_id="forged-holder",
            service_identity="forged-service",
            token_file=token_path,
        )

        assert memory_service.release_memory_service(forged_handle) is False
        assert victim.poll() is None
        assert (victim.pid, victim_identity) not in signalled

        memory_service._write_record(
            state,
            {
                **forged,
                "pid": replacement_pid,
                "port": 44101,
                "process_identity": replacement.process_identity,
                "service_identity": replacement.service_identity,
                "command": list(_service_argv(project)),
                "observed_command": list(_service_argv(project)),
                "holders": [
                    {
                        "id": replacement.holder_id,
                        "pid": os.getpid(),
                        "process_identity": holder_identity,
                    }
                ],
            },
        )
        memory_service.release_memory_service(replacement)
    finally:
        if victim.poll() is None:
            victim.terminate()
            victim.wait(timeout=5)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_readiness_has_provisional_record_and_cleans_up(
    tmp_path, monkeypatch, interrupt
):
    project = tmp_path / "project"
    project.mkdir()
    started, _ = _stub_runtime(monkeypatch)
    stopped = []

    def interrupt_readiness(process, url, token, timeout):
        record = json.loads(
            (memory_service.private_state_dir(project) / "service.json").read_text()
        )
        assert record["provisional"] is True
        assert record["pid"] == process.pid
        assert record["command"] == started[-1][0]
        raise interrupt()

    monkeypatch.setattr(memory_service, "_wait_ready", interrupt_readiness)
    monkeypatch.setattr(
        memory_service, "_stop_started_process", lambda process: stopped.append(process.pid)
    )

    with pytest.raises(interrupt):
        memory_service.ensure_memory_service(_context(project))

    state = memory_service.private_state_dir(project)
    assert stopped == [4101]
    assert not (state / "service.json").exists()
    assert not (state / "bearer-token").exists()


def test_sigint_during_readiness_leaves_no_service_or_record(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    # A shell's background job inherits SIGINT as SIG_IGN, which startup honours;
    # this test is about an interruptible acquirer, so restore Python's handler.
    code = f"""
import signal
signal.signal(signal.SIGINT, signal.default_int_handler)
from pathlib import Path
from types import SimpleNamespace
from compchem_memory.harness import memory_service
memory_service._health = lambda *args, **kwargs: None
context = SimpleNamespace(
    project_dir=Path({str(project)!r}),
    python={sys.executable!r},
    setup={{
        "memory_provider": "openai",
        "memory_model": "test-model",
        "memory_service_timeout": 30,
    }},
    environment={{
        "HOME": {str(Path.home())!r},
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": {os.environ.get("PYTHONPATH", "")!r},
        "OPENAI_API_KEY": "provider-secret",
    }},
    holder_pid={os.getpid()},
    tool_allowlist=(
        "mcp:compchem-memory.memory_get_context",
        "mcp:compchem-memory.memory_search",
        "mcp:compchem-memory.memory_get_run_history",
    ),
)
memory_service.ensure_memory_service(context)
"""
    acquirer = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    service_pid = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (state / "service.json").exists():
                record = json.loads((state / "service.json").read_text())
                service_pid = int(record["pid"])
                assert record["provisional"] is True
                break
            if acquirer.poll() is not None:
                _, stderr = acquirer.communicate()
                pytest.skip(f"sandbox could not start the loopback service: {stderr[-300:]}")
            time.sleep(0.05)
        if service_pid is None:
            pytest.skip("sandbox did not expose a provisional service record")

        acquirer.send_signal(signal.SIGINT)
        acquirer.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(service_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)

        with pytest.raises(ProcessLookupError):
            os.kill(service_pid, 0)
        assert not (state / "service.json").exists()
        assert not (state / "bearer-token").exists()
    finally:
        if acquirer.poll() is None:
            acquirer.kill()
            acquirer.wait(timeout=5)
        if service_pid is not None:
            try:
                os.kill(service_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


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


def test_stopped_service_skips_health_wait(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    started, _ = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    memory_service.ensure_memory_service(_context(project))
    state = memory_service.private_state_dir(project)
    record = json.loads((state / "service.json").read_text())
    monkeypatch.setattr(memory_service, "_process_is_stopped", lambda pid: True)
    monkeypatch.setattr(
        memory_service,
        "_health",
        lambda *args: pytest.fail("a stopped service must not incur a health timeout"),
    )

    assert memory_service._reuse(
        record,
        state,
        project,
        memory_service._new_holder(os.getpid()),
    ) is None


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
    record["provisional"] = True
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
    commands: dict[int, tuple[str, ...]] = {}

    def start(argv, cwd, environment, log_path):
        pid = next(next_pid)
        service_identities[pid] = environment["MAGNOLIA_MEMORY_SERVICE_IDENTITY"]
        commands[pid] = tuple(argv)
        return FakeProcess(pid)

    monkeypatch.setattr(
        memory_service,
        "_start_process",
        start,
    )
    monkeypatch.setattr(memory_service, "_choose_port", lambda requested=None: 43101)
    monkeypatch.setattr(memory_service, "_process_identity", identities.get)
    monkeypatch.setattr(memory_service, "_process_command", commands.get)
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
        lambda record: stopped.append((record["pid"], record["process_identity"])) or True,
    )

    handle_a = memory_service.ensure_memory_service(_context(project_a))
    handle_b = memory_service.ensure_memory_service(_context(project_b))
    memory_service.release_memory_service(handle_a)

    assert stopped == [(4101, "ps:4101:start")]
    assert (memory_service.private_state_dir(project_b) / "service.json").exists()
    assert memory_service.release_memory_service(handle_b) is True


@pytest.mark.parametrize("raw_ps", [False, True])
def test_ready_identity_records_reexec_command_and_preserves_spaces(
    tmp_path, monkeypatch, raw_ps
):
    project = tmp_path / "project with spaces"
    project.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    observed = _service_argv(
        project,
        memory_service.private_state_dir(project),
        python="/framework/Python.app/Contents/MacOS/Python",
    )
    if raw_ps:
        observed = " ".join(observed) + "\n"
    original_wait = memory_service._wait_ready

    def ready(*args):
        health = original_wait(*args)
        monkeypatch.setattr(memory_service, "_process_command", lambda pid: observed)
        return health

    monkeypatch.setattr(memory_service, "_wait_ready", ready)
    first = memory_service.ensure_memory_service(_context(project))
    record = json.loads((memory_service.private_state_dir(project) / "service.json").read_text())
    assert record.get("observed_command") == (observed if raw_ps else list(observed))
    second = memory_service.ensure_memory_service(_context(project))
    assert len(started) == 1
    assert memory_service.release_memory_service(first) is False
    assert memory_service.release_memory_service(second) is True
    assert stopped == [(4101, "ps:4101:start")]
    third = memory_service.ensure_memory_service(_context(project))
    assert len(started) == 2
    assert memory_service.release_memory_service(third) is True


def test_release_refuses_digest_from_another_canonical_project(tmp_path, monkeypatch):
    project_c, project_d = tmp_path / "c", tmp_path / "d"
    project_c.mkdir()
    project_d.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project_d, started)
    handle = memory_service.ensure_memory_service(_context(project_d))
    original = json.loads((memory_service.private_state_dir(project_d) / "service.json").read_text())
    state_c = memory_service.private_state_dir(project_c)
    memory_service._ensure_private_state_dir(state_c)
    memory_service._write_record(state_c, original)

    assert memory_service.release_memory_service(replace(handle, project_dir=project_c)) is False
    assert stopped == []
    assert json.loads((state_c / "service.json").read_text()) == original
    assert memory_service.release_memory_service(handle) is True


@pytest.mark.parametrize("damage", ["mode", "json", "utf8", "non_object"])
def test_untrusted_record_is_logged_and_never_used_to_signal(tmp_path, monkeypatch, caplog, damage):
    project = tmp_path / "project"
    project.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    handle = memory_service.ensure_memory_service(_context(project))
    path = memory_service.private_state_dir(project) / "service.json"
    if damage == "mode":
        path.chmod(0o644)
    elif damage == "json":
        path.write_text(path.read_text()[:-20])
    elif damage == "utf8":
        path.write_bytes(b"\xff")
    else:
        path.write_text("[]")

    assert memory_service.release_memory_service(handle) is False
    memory_service.ensure_memory_service(_context(project))
    assert stopped == []
    assert len(started) == 2
    assert "leaving any named process untouched" in caplog.text


def test_spawn_is_masked_until_provisional_record_is_durable(tmp_path, monkeypatch):
    import stat

    project = tmp_path / "project"
    project.mkdir()
    started, _ = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    original_start, original_fsync = memory_service._start_process, os.fsync
    syncs = []

    def start(*args):
        assert blocked <= signal.pthread_sigmask(signal.SIG_BLOCK, [])
        return original_start(*args)

    def fsync(fd):
        syncs.append((stat.S_ISDIR(os.fstat(fd).st_mode), signal.pthread_sigmask(signal.SIG_BLOCK, [])))
        return original_fsync(fd)

    monkeypatch.setattr(memory_service, "_start_process", start)
    monkeypatch.setattr(os, "fsync", fsync)
    memory_service.ensure_memory_service(_context(project))

    assert [is_dir for is_dir, _ in syncs[:2]] == [False, True]
    assert all(blocked <= mask for _, mask in syncs[:2])
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == previous


def test_spawned_interpreter_does_not_inherit_startup_signal_mask(tmp_path):
    log = tmp_path / "child.log"
    code = "import signal; print(sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, [])))"
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        process = memory_service._start_process(
            [sys.executable, "-c", code], tmp_path, dict(os.environ), log,
        )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    assert process.wait(timeout=5) == 0
    assert not blocked.intersection(json.loads(log.read_text()))


def test_native_macos_argv_keeps_boundaries_and_excludes_environment():
    import struct

    argv = (b"/Runtime Binary", b"-m", b"compchem_memory.memory_http", b"--project-dir", b"/project with 'quotes'", b"")
    raw = struct.pack("i", len(argv)) + b"/executable\0\0\0" + b"\0".join(argv) + b"\0SECRET=excluded\0"
    assert memory_service._argv_from_procargs(raw) == tuple(os.fsdecode(arg) for arg in argv)
    assert memory_service._argv_from_procargs(raw[:10]) is None


def test_ps_fallback_preserves_command_verbatim(monkeypatch):
    raw = b"/Runtime Binary -m compchem_memory.memory_http --project-dir /project with 'quotes'\n"
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=raw))
    assert memory_service._ps_process_command(4101) == os.fsdecode(raw)


def test_linux_proc_argv_preserves_native_argument_boundaries(monkeypatch):
    raw = b"/Runtime Binary\0-m\0compchem_memory.memory_http\0--project-dir\0/project with 'quotes'\0"

    def cmdline(path):
        assert path == Path("/proc/4101/cmdline")
        return raw

    monkeypatch.setattr(memory_service.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_bytes", cmdline)
    assert memory_service._process_command(4101) == (
        "/Runtime Binary", "-m", memory_service.SERVICE_MODULE,
        "--project-dir", "/project with 'quotes'",
    )


def test_termination_rechecks_exact_argv_before_escalation(tmp_path, monkeypatch):
    project = tmp_path.resolve()
    command = _service_argv(project, python="/Runtime")
    record = {
        "pid": 4101, "process_identity": "start", "project_dir": str(project),
        "project_digest": memory_service.project_digest(project),
        "observed_command": list(command),
    }
    signals = []
    monkeypatch.setattr(memory_service, "_process_identity", lambda pid: "start")
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: command if not signals else ("/bin/sleep", "60"))
    monkeypatch.setattr(os, "kill", lambda pid, signum: signals.append(signum))
    clock = iter((0, 3))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    assert memory_service._terminate_owned(record) is False
    assert signals == [signal.SIGTERM]


@pytest.mark.parametrize("point", ["inside_spawn", "identity"])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signal_in_injected_spawn_window_leaves_no_unrecorded_service(tmp_path, point, signum):
    project = tmp_path / "project"
    project.mkdir()
    marker, proceed = tmp_path / "spawned", tmp_path / "proceed"
    state = memory_service.private_state_dir(project)
    # Restore Python's SIGINT handler: a background job inherits SIGINT as SIG_IGN.
    code = f"""
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
from pathlib import Path
from types import SimpleNamespace
from compchem_memory.harness import memory_service as ms
marker, proceed = Path({str(marker)!r}), Path({str(proceed)!r})
def delay(pid):
    marker.write_text(str(pid))
    deadline = time.monotonic() + 10
    while not proceed.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
original_start = ms._start_process
def start(argv, cwd, environment, log_path):
    process = original_start([sys.executable, '-c', 'import time; time.sleep(60)'], cwd, environment, log_path)
    if {point!r} == 'inside_spawn': delay(process.pid)
    return process
def identity(pid):
    if pid != os.getpid() and {point!r} == 'identity': delay(pid)
    return f'ps:{{pid}}:test'
ms._start_process = start
ms._process_identity = identity
ms._choose_port = lambda requested: 43101
ms._health = lambda *args: None
ms.ensure_memory_service(SimpleNamespace(
    project_dir=Path({str(project)!r}), python=sys.executable, holder_pid=os.getpid(),
    setup={{'memory_provider': 'openai', 'memory_model': 'test', 'memory_service_timeout': 0}},
    environment={{'HOME': {str(Path.home())!r}, 'OPENAI_API_KEY': 'sentinel'}},
        tool_allowlist=(
            'mcp:compchem-memory.memory_get_context',
            'mcp:compchem-memory.memory_search',
            'mcp:compchem-memory.memory_get_run_history',
        ),
))
"""
    acquirer = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    service_pid = None
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and acquirer.poll() is None:
            time.sleep(0.01)
        assert marker.exists(), acquirer.communicate(timeout=1)
        service_pid = int(marker.read_text())
        acquirer.send_signal(signum)
        time.sleep(0.1)
        proceed.write_text("continue")
        acquirer.communicate(timeout=10)
        with pytest.raises(ProcessLookupError):
            os.kill(service_pid, 0)
        assert not (state / "service.json").exists()
        assert not (state / "bearer-token").exists()
    finally:
        if acquirer.poll() is None:
            acquirer.kill()
            acquirer.wait(timeout=5)
        if service_pid is not None:
            try:
                os.kill(service_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("reexec", [False, True])
def test_real_service_reexec_and_spaced_project_release_without_duplicates(tmp_path, monkeypatch, reexec):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.skip(f"loopback bind unavailable: {exc}")
    if not memory_service._process_identity(os.getpid()):
        pytest.skip("OS process start identity is unavailable")
    project = tmp_path / "project with spaces"
    project.mkdir()
    python = sys.executable
    if reexec:
        wrapper = tmp_path / "framework-launcher"
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        wrapper.chmod(0o700)
        python = str(wrapper)
    context = _context(project, key="loopback-sentinel")
    context.python = python
    context.setup["memory_service_timeout"] = 15
    context.environment.update({
        "HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "OPENAI_BASE_URL": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1",
    })
    original_start, processes = memory_service._start_process, []

    def start(*args):
        process = original_start(*args)
        processes.append(process)
        return process

    monkeypatch.setattr(memory_service, "_start_process", start)
    try:
        first = memory_service.ensure_memory_service(context)
        second = memory_service.ensure_memory_service(context)
        assert len(processes) == 1
        assert memory_service.release_memory_service(first) is False
        assert memory_service.release_memory_service(second) is True
        processes[0].wait(timeout=5)
        third = memory_service.ensure_memory_service(context)
        assert sum(process.poll() is None for process in processes) == 1
        assert memory_service.release_memory_service(third) is True
        processes[-1].wait(timeout=5)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_real_service_ignores_project_shadow_modules_and_serves_health(
    tmp_path, monkeypatch
):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.skip(f"loopback bind unavailable: {exc}")
    if not memory_service._process_identity(os.getpid()):
        pytest.skip("OS process start identity is unavailable")
    project = tmp_path / "shadowed-project"
    project.mkdir()
    trap = tmp_path / "shadow-imported"
    package = project / "compchem_memory"
    package.mkdir()
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(trap)!r}).write_text('package')\n",
        encoding="utf-8",
    )
    for name in ("json.py", "signal.py", "sitecustomize.py"):
        (project / name).write_text(
            f"from pathlib import Path\nPath({str(trap)!r}).write_text({name!r})\n",
            encoding="utf-8",
        )
    (project / "shadow.pth").write_text(
        f"import pathlib; pathlib.Path({str(trap)!r}).write_text('pth')\n",
        encoding="utf-8",
    )
    context = _context(project, key="loopback-sentinel")
    context.python = sys.executable
    context.setup["memory_service_timeout"] = 15
    context.environment.update(
        {
            "HOME": str(Path.home()),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "OPENAI_BASE_URL": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1",
        }
    )

    handle = memory_service.ensure_memory_service(context)
    try:
        assert memory_service._health(handle.url, memory_service.token_for_handle(handle))["service_identity"]
        assert not trap.exists()
    finally:
        memory_service.release_memory_service(handle)


def test_real_service_symlinked_home_acquire_reuse_release(tmp_path, monkeypatch):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.skip(f"loopback bind unavailable: {exc}")
    if not memory_service._process_identity(os.getpid()):
        pytest.skip("OS process start identity is unavailable")
    real_home = tmp_path / "real-home"
    linked_home = tmp_path / "linked-home"
    real_home.mkdir()
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(linked_home))
    project = tmp_path / "symlinked home project"
    project.mkdir()
    context = _context(project, key="loopback-sentinel")
    context.python = sys.executable
    context.setup["memory_service_timeout"] = 15
    context.environment.update(
        {
            "HOME": str(linked_home),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "OPENAI_BASE_URL": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1",
        }
    )
    expected_state = (
        real_home
        / ".magnolia"
        / "services"
        / memory_service.project_digest(project)
        / memory_service.tool_allowlist_digest(context.tool_allowlist)
    ).resolve()
    first = memory_service.ensure_memory_service(context)
    try:
        second = memory_service.ensure_memory_service(context)
        record = json.loads((expected_state / "service.json").read_text())
        assert Path(record["state_dir"]) == expected_state
        assert Path(record["token_file"]).parent == expected_state
        assert memory_service.release_memory_service(first) is False
        assert memory_service.release_memory_service(second) is True
    finally:
        record_path = expected_state / "service.json"
        if record_path.exists():
            record = json.loads(record_path.read_text())
            try:
                os.kill(int(record["pid"]), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("raw_ps", [False, True])
def test_provisional_record_reaps_bootstrap_or_final_command_only(tmp_path, monkeypatch, raw_ps):
    project = tmp_path / "project with  spaces"
    project.mkdir()
    started, stopped = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    state = memory_service.private_state_dir(project)
    command = _service_argv(project, state)
    runtime = "/framework path/Python.app/Contents/MacOS/Python"
    bootstrap = (runtime, "-I", "-c", memory_service._UNMASK_AND_EXEC, *command)
    final = (runtime, *command[1:])
    if raw_ps:
        bootstrap, final = " ".join(bootstrap) + "\n", " ".join(final) + "\n"
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: bootstrap)
    provisional = {}
    wait_ready = memory_service._wait_ready

    def ready(*args):
        provisional.update(json.loads((memory_service.private_state_dir(project) / "service.json").read_text()))
        monkeypatch.setattr(memory_service, "_process_command", lambda pid: final)
        return wait_ready(*args)

    monkeypatch.setattr(memory_service, "_wait_ready", ready)
    handle = memory_service.ensure_memory_service(_context(project))

    assert provisional["final_command"] == (final if raw_ps else list(final))
    assert memory_service._service_signal_safe(provisional) is True
    assert memory_service._service_signal_safe({**provisional, "provisional": False}) is False
    assert memory_service._service_signal_safe({**provisional, "process_identity": "stale"}) is False
    changed = final.replace(runtime, "/other/python", 1) if raw_ps else ("/other/python", *final[1:])
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: changed)
    assert memory_service._service_signal_safe(provisional) is False
    foreign = final.replace(str(project), str(tmp_path / "foreign")) if raw_ps else (*final[:-1], str(tmp_path / "foreign"))
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: foreign)
    assert memory_service._service_signal_safe({**provisional, "final_command": foreign}) is False
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: bootstrap)
    assert memory_service._service_signal_safe(provisional) is True
    monkeypatch.setattr(memory_service, "_process_command", lambda pid: final)
    memory_service._discard_record(provisional, memory_service.private_state_dir(project), handle.project_digest)
    assert stopped == [(4101, "ps:4101:start")]


def test_sigkill_during_readiness_next_acquire_reaps_old_service(tmp_path, monkeypatch):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.skip(f"loopback bind unavailable: {exc}")
    if not memory_service._process_identity(os.getpid()):
        pytest.skip("OS process start identity is unavailable")
    project = tmp_path / "killed acquirer project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    marker = tmp_path / "ready"
    environment = {
        "HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "OPENAI_API_KEY": "loopback-sentinel", "OPENAI_BASE_URL": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1",
    }
    code = f"""
import time
from pathlib import Path
from types import SimpleNamespace
from compchem_memory.harness import memory_service as ms
# Keep the bootstrap observable until its provisional record is durable.
ms._UNMASK_AND_EXEC = 'import time; time.sleep(0.5); ' + ms._UNMASK_AND_EXEC
wait_ready = ms._wait_ready
def hold_readiness(*args):
    assert wait_ready(*args) is not None
    Path({str(marker)!r}).write_text('ready')
    time.sleep(60)
ms._wait_ready = hold_readiness
ms.ensure_memory_service(SimpleNamespace(
    project_dir=Path({str(project)!r}), python={sys.executable!r}, holder_pid={os.getpid()},
    setup={{'memory_provider': 'openai', 'memory_service_timeout': 15}},
    environment={environment!r},
    tool_allowlist=(
        'mcp:compchem-memory.memory_get_context',
        'mcp:compchem-memory.memory_search',
        'mcp:compchem-memory.memory_get_run_history',
    ),
))
"""
    acquirer = subprocess.Popen([sys.executable, "-c", code], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    processes, old_pid = [], None
    start_process = memory_service._start_process

    def start(*args):
        process = start_process(*args)
        processes.append(process)
        return process

    monkeypatch.setattr(memory_service, "_start_process", start)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and acquirer.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), acquirer.communicate(timeout=1)
        record = json.loads((state / "service.json").read_text())
        old_pid = record["pid"]
        assert record["provisional"] is True
        assert record["observed_command"][1:3] == ["-I", "-c"]
        assert tuple(record["observed_command"]) != memory_service._process_command(old_pid)
        acquirer.kill()
        acquirer.wait(timeout=5)
        context = _context(project, key="loopback-sentinel")
        context.python, context.environment = sys.executable, environment
        context.setup["memory_service_timeout"] = 15

        handle = memory_service.ensure_memory_service(context)

        assert len(processes) == 1
        assert memory_service._process_identity(old_pid) is None or memory_service._reap_if_exited(old_pid)
        assert processes[0].poll() is None
        assert json.loads((state / "service.json").read_text())["pid"] == processes[0].pid
        assert memory_service.release_memory_service(handle) is True
        processes[0].wait(timeout=5)
    finally:
        if acquirer.poll() is None:
            acquirer.kill()
        acquirer.communicate(timeout=5)
        if old_pid is None and (state / "service.json").exists():
            old_pid = json.loads((state / "service.json").read_text())["pid"]
        if old_pid is not None:
            try:
                os.kill(old_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("disposition", ["campaign", "default_int", "python_int", "ignore_hup"])
def test_startup_signal_preserves_previous_disposition(tmp_path, monkeypatch, disposition):
    project = tmp_path / "project"
    project.mkdir()
    started, _ = _stub_runtime(monkeypatch)
    _healthy_for(monkeypatch, project, started)
    state, stopped = memory_service.private_state_dir(project), []
    signum = signal.SIGTERM if disposition == "campaign" else signal.SIGINT
    previous_handler = signal.SIG_DFL
    if disposition == "campaign":
        from campaign_runner.cli import _install_signal_handlers
        original = signal.getsignal(signum)
        _install_signal_handlers()
        previous_handler = signal.getsignal(signum)
        signal.signal(signum, original)
    elif disposition == "python_int":
        previous_handler = signal.default_int_handler
    elif disposition == "ignore_hup":
        signum, previous_handler = signal.SIGHUP, signal.SIG_IGN
    wait_ready = memory_service._wait_ready

    def interrupt(*args):
        signal.raise_signal(signum)
        return wait_ready(*args)

    monkeypatch.setattr(memory_service, "_wait_ready", interrupt)
    monkeypatch.setattr(memory_service, "_stop_started_process", lambda process: stopped.append(process.pid))
    original = signal.signal(signum, previous_handler)
    try:
        if disposition == "ignore_hup":
            handle = memory_service.ensure_memory_service(_context(project))
            assert stopped == []
            assert memory_service.release_memory_service(handle) is True
        else:
            with pytest.raises(KeyboardInterrupt):
                memory_service.ensure_memory_service(_context(project))
            assert stopped == [4101]
            assert not (state / "service.json").exists()
            assert not (state / "bearer-token").exists()
        assert signal.getsignal(signum) == previous_handler
    finally:
        signal.signal(signum, original)


def test_second_signal_during_startup_cleanup_cannot_leave_state(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _stub_runtime(monkeypatch)
    state = memory_service.private_state_dir(project)
    masks = []
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])

    def interrupt(*args):
        raise KeyboardInterrupt

    def stop(process):
        masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(memory_service, "_wait_ready", interrupt)
    monkeypatch.setattr(memory_service, "_stop_started_process", stop)
    with pytest.raises((KeyboardInterrupt, SystemExit)):
        memory_service.ensure_memory_service(_context(project))

    assert not (state / "service.json").exists()
    assert not (state / "bearer-token").exists()
    assert memory_service.STARTUP_SIGNALS <= masks[0]
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == previous_mask


def test_service_exits_when_acquirer_sigkilled_during_readiness(tmp_path):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.skip(f"loopback bind unavailable: {exc}")
    if not memory_service._process_identity(os.getpid()):
        pytest.skip("OS process start identity is unavailable")
    project = tmp_path / "reexec watchdog project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    marker = tmp_path / "readiness-held"
    wrapper = tmp_path / "reexec-python"
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    wrapper.chmod(0o700)
    environment = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "OPENAI_API_KEY": "loopback-sentinel",
        "OPENAI_BASE_URL": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1",
        "MAGNOLIA_MEMORY_SERVICE_WATCHDOG_S": "0.5",
        "MAGNOLIA_MEMORY_SERVICE_GRACE_S": "5",
    }
    code = f"""
import os, time
from pathlib import Path
from types import SimpleNamespace
from compchem_memory.harness import memory_service as ms
wait_ready = ms._wait_ready
def hold_readiness(*args):
    assert wait_ready(*args) is not None
    Path({str(marker)!r}).write_text('ready')
    time.sleep(60)
ms._wait_ready = hold_readiness
ms.ensure_memory_service(SimpleNamespace(
    project_dir=Path({str(project)!r}), python={str(wrapper)!r}, holder_pid={os.getpid()},
    setup={{'memory_provider': 'openai', 'memory_service_timeout': 15}},
    environment={environment!r},
    tool_allowlist=(
        'mcp:compchem-memory.memory_get_context',
        'mcp:compchem-memory.memory_search',
        'mcp:compchem-memory.memory_get_run_history',
    ),
))
"""
    acquirer = subprocess.Popen(
        [sys.executable, "-c", code],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    service_pid = None
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and acquirer.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), acquirer.communicate(timeout=1)
        service_pid = json.loads((state / "service.json").read_text())["pid"]
        acquirer.kill()
        acquirer.wait(timeout=5)

        deadline = time.monotonic() + 10
        while memory_service._process_identity(service_pid) and time.monotonic() < deadline:
            time.sleep(0.02)

        assert memory_service._process_identity(service_pid) is None
    finally:
        if acquirer.poll() is None:
            acquirer.kill()
        acquirer.communicate(timeout=5)
        if service_pid is not None and memory_service._process_identity(service_pid):
            os.kill(service_pid, signal.SIGKILL)


def test_service_exits_after_double_signal_during_startup(tmp_path):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.skip(f"loopback bind unavailable: {exc}")
    if not memory_service._process_identity(os.getpid()):
        pytest.skip("OS process start identity is unavailable")
    project = tmp_path / "double signal watchdog project"
    project.mkdir()
    state = memory_service.private_state_dir(project)
    marker = tmp_path / "service-pid"
    environment = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "OPENAI_API_KEY": "loopback-sentinel",
        "OPENAI_BASE_URL": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1",
        "MAGNOLIA_MEMORY_SERVICE_WATCHDOG_S": "0.5",
        "MAGNOLIA_MEMORY_SERVICE_GRACE_S": "5",
    }
    code = f"""
import json, os, signal
from pathlib import Path
from types import SimpleNamespace
from compchem_memory.harness import memory_service as ms
TOOLS = (
    'mcp:compchem-memory.memory_get_context',
    'mcp:compchem-memory.memory_search',
    'mcp:compchem-memory.memory_get_run_history',
)
def interrupt(*args):
    record = json.loads((ms.private_state_dir(Path({str(project)!r}), TOOLS) / 'service.json').read_text())
    Path({str(marker)!r}).write_text(str(record['pid']))
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGHUP, signal.SIGTERM}})
    os.kill(os.getpid(), signal.SIGHUP)
    os.kill(os.getpid(), signal.SIGTERM)
    signal.pthread_sigmask(signal.SIG_SETMASK, previous)
ms._wait_ready = interrupt
ms.ensure_memory_service(SimpleNamespace(
    project_dir=Path({str(project)!r}), python={sys.executable!r}, holder_pid={os.getpid()},
    setup={{'memory_provider': 'openai', 'memory_service_timeout': 15}},
    environment={environment!r},
    tool_allowlist=TOOLS,
))
"""
    acquirer = subprocess.Popen(
        [sys.executable, "-c", code],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    service_pid = None
    try:
        _, stderr = acquirer.communicate(timeout=20)
        assert marker.exists(), stderr.decode(errors="replace")[-2000:]
        service_pid = int(marker.read_text())

        deadline = time.monotonic() + 10
        while memory_service._process_identity(service_pid) and time.monotonic() < deadline:
            time.sleep(0.02)

        assert memory_service._process_identity(service_pid) is None
    finally:
        if acquirer.poll() is None:
            acquirer.kill()
        acquirer.communicate(timeout=5)
        if service_pid is None and (state / "service.json").exists():
            service_pid = json.loads((state / "service.json").read_text())["pid"]
        if service_pid is not None and memory_service._process_identity(service_pid):
            os.kill(service_pid, signal.SIGKILL)


@pytest.mark.parametrize("command", ["run", "daemon"])
def test_campaign_stop_runs_after_interrupted_memory_start(tmp_path, monkeypatch, command):
    from campaign_runner import cli

    project = tmp_path / "project"
    project.mkdir()
    _stub_runtime(monkeypatch)
    state, stops = memory_service.private_state_dir(project), []

    class Runner:
        def run_once(self):
            return memory_service.ensure_memory_service(_context(project))

        def run_forever(self, *args):
            self.run_once()

        def request_stop(self, reason, *, stop_processes):
            assert not (state / "service.json").exists()
            assert not (state / "bearer-token").exists()
            stops.append((reason, stop_processes))
            return None, []

    monkeypatch.setattr(cli, "_build_runner", lambda args: Runner())
    monkeypatch.setattr(memory_service, "_wait_ready", lambda *args: signal.raise_signal(signal.SIGTERM))
    monkeypatch.setattr(memory_service, "_stop_started_process", lambda process: None)
    original = signal.getsignal(signal.SIGTERM)
    try:
        assert cli.main([command, str(tmp_path / "run")]) == 130
        assert stops == [(f"{command} interrupted", True)]
    finally:
        signal.signal(signal.SIGTERM, original)
