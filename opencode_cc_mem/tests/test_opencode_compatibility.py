import fcntl
import importlib.util
import json
import multiprocessing
import os
import resource
import signal
import subprocess
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.process import ProcessControl, ProcessOutcome, run_process
from campaign_runner.reviewers import Reviewer
from campaign_runner.routing import resolve_route
from campaign_runner.workers import OpenCodeExecutor


def test_direct_opencode_client_calls_disconnect_stdin(monkeypatch, tmp_path):
    from campaign_runner.executors import opencode as opencode_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(opencode_module.subprocess, "run", fake_run)
    helper = opencode_module.OpenCodeHelperExecutor("opencode", "provider/model")
    evaluator = opencode_module.OpenCodeEvaluatorExecutor("opencode", "provider/model")
    helper.run([], cwd=tmp_path, environment={}, timeout_seconds=1)
    evaluator.run("prompt", environment={}, timeout_seconds=1)
    evaluator.catalog(environment={}, timeout_seconds=1)

    assert len(calls) == 3
    assert all(kwargs.get("stdin") is subprocess.DEVNULL for _, kwargs in calls)


def _route(role):
    inputs = {
        "main_model": "provider/model",
        "reviewer": {"model": "provider/model"},
        "agent": {"json": {"model": "provider/model"}},
        "model": "provider/model",
    }
    return resolve_route(role, inputs)


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "tests" / "snapshots" / "opencode"


def _snapshot(name):
    return json.loads((SNAPSHOTS / name).read_text())


def _normalise(values, tmp_path):
    return [value.replace(str(tmp_path), "@@TMP@@") for value in values]


def _successful_client(kwargs):
    kwargs["stdout_path"].write_text(
        json.dumps(
            {
                "sessionID": "ses_recipe",
                "part": {
                    "type": "text",
                    "messageID": "m1",
                    "text": '{"issues":[],"followups":[]}',
                },
            }
        )
        + "\n"
    )
    kwargs["stderr_path"].write_text("")
    return ProcessOutcome(
        0, False, 0.1, kwargs["stdout_path"], kwargs["stderr_path"]
    )


def _load_agents_daemon():
    path = ROOT / "softwares" / "bin" / "magnolia-agents-daemon"
    spec = importlib.util.spec_from_file_location(
        "compat_agents_daemon",
        path,
        loader=SourceFileLoader("compat_agents_daemon", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opencode_recipes_unchanged_through_registry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from campaign_runner import reviewers as reviewers_module
    from campaign_runner import service as service_module
    from campaign_runner import workers as workers_module
    from campaign_runner.cli import _service_settings
    from campaign_runner.executors import opencode as opencode_module
    from router_runner import evaluator as evaluator_module

    process_calls = []

    def fake_run_process(**kwargs):
        process_calls.append(kwargs)
        return _successful_client(kwargs)

    monkeypatch.setattr(workers_module.os, "environ", {"BASE_ENV": "ignored"})
    monkeypatch.setattr(workers_module, "run_process", fake_run_process)
    monkeypatch.setattr(
        reviewers_module,
        "read_session_usage",
        lambda *args: workers_module.empty_usage(),
    )

    worker_route = resolve_route(
        "worker",
        {"worker": {"model": "opencode-go/deepseek-v4.1-flash"}},
    )
    worker = executor_for(
        worker_route,
        "worker",
        ExecContext(
            route=worker_route,
            role="worker",
            project_dir=tmp_path,
            timeout_seconds=37,
            opencode_binary=str(tmp_path / "opencode"),
            opencode_server="http://127.0.0.1:4111",
            opencode_db=str(tmp_path / "opencode.db"),
            agent="campaign-worker",
            environment={"RECIPE_EXTRA": "ignored"},
        ),
    )
    worker.run("PINNED PROMPT", tmp_path, tmp_path / "worker")
    worker_call = process_calls[-1]
    assert {
        "argv": _normalise(worker_call["argv"], tmp_path),
        "environment_keys": sorted(worker_call["env"]),
        "step_cap_config": worker_call["env"].get("OPENCODE_CONFIG_CONTENT"),
        "timeout_seconds": worker_call["timeout_seconds"],
        "agent_file_bytes": (
            ROOT / ".opencode" / "agents" / "campaign-worker.md"
        ).read_text(),
    } == _snapshot("campaign-worker.json")

    reviewer = Reviewer(
        binary=str(tmp_path / "opencode"),
        server_url="http://127.0.0.1:4111",
        db_path=str(tmp_path / "opencode.db"),
        environment={"RECIPE_EXTRA": "ignored"},
        timeout_seconds=55,
        max_steps=8,
    )
    reviewer.run(
        "provider/model", "REVIEW", tmp_path, tmp_path / "reviewer", tmp_path
    )
    reviewer_call = process_calls[-1]
    assert {
        "argv": _normalise(reviewer_call["argv"], tmp_path),
        "environment_keys": sorted(reviewer_call["env"]),
        "step_cap_config": json.loads(
            reviewer_call["env"]["OPENCODE_CONFIG_CONTENT"]
        ),
        "timeout_seconds": reviewer_call["timeout_seconds"],
        "agent_file_bytes": (
            ROOT / ".opencode" / "agents" / "campaign-reviewer.md"
        ).read_text(),
    } == _snapshot("campaign-reviewer.json")

    service_seen = {}

    class FakeService:
        pid = 424242

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        service_seen.update(argv=argv, environment=kwargs["env"])
        return FakeService()

    real_ensure_service = service_module.ensure_service

    def capture_ensure_service(*args, **kwargs):
        service_seen["timeout_seconds"] = kwargs["timeout_seconds"]
        return real_ensure_service(*args, **kwargs)

    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service_module, "_choose_port", lambda: 4137)
    monkeypatch.setattr(service_module, "_healthy", lambda *args: True)
    monkeypatch.setattr(
        service_module,
        "_wait_for_agents",
        lambda port, password, names, **kwargs:
            service_seen.setdefault("required_agents", list(names)) is not None,
    )
    monkeypatch.setattr(service_module, "redact_file", lambda path: None)
    monkeypatch.setattr(opencode_module, "ensure_service", capture_ensure_service)
    protocol = SimpleNamespace(
        worker=SimpleNamespace(max_steps=30),
        reviewer=SimpleNamespace(enabled=True, max_steps=8),
    )
    config, required_agents = _service_settings(protocol)
    service = executor_for(
        _route("worker"),
        "service",
        ExecContext(
            route=_route("worker"),
            role="service",
            project_dir=tmp_path,
            opencode_binary=str(tmp_path / "opencode"),
            opencode_db=str(tmp_path / "opencode.db"),
        ),
    )
    service.ensure(
        config_content=config,
        required_agents=required_agents,
        timeout_seconds=30,
    )
    assert {
        "argv": _normalise(service_seen["argv"], tmp_path),
        "environment_keys": sorted(service_seen["environment"]),
        "required_agents": service_seen["required_agents"],
        "startup_steps": {
            name: value["steps"]
            for name, value in json.loads(config)["agents"].items()
        },
        "timeout_seconds": service_seen["timeout_seconds"],
        "agent_file_bytes": {
            name: (ROOT / ".opencode" / "agents" / name).read_text()
            for name in ("campaign-worker.md", "campaign-reviewer.md")
        },
    } == _snapshot("service.json")

    subprocess_calls = []

    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_subprocess_run(argv, **kwargs):
        subprocess_calls.append((argv, kwargs))
        if "/api/model" in argv:
            return Completed('{"data":[{"id":"jev-1.13-free"}]}')
        if "--standalone" in argv:
            return Completed(
                '{"action":"accept","project":"proj_a",'
                '"confidence":0.92,"reason":"dock"}'
            )
        return Completed(
            json.dumps(
                {
                    "sessionID": "ses_helper",
                    "part": {
                        "type": "text",
                        "messageID": "m1",
                        "text": "FINAL ANSWER: complete",
                    },
                }
            )
            + "\n"
        )

    daemon = _load_agents_daemon()
    monkeypatch.setattr(daemon.subprocess, "run", fake_subprocess_run)
    daemon._run_once(
        "provider/model",
        str(tmp_path),
        {"BASE_ENV": "ignored"},
        60,
        ["PROMPT"],
        None,
        binary=str(tmp_path / "opencode"),
    )
    helper_argv, helper_kwargs = subprocess_calls[-1]
    assert {
        "argv": _normalise(helper_argv, tmp_path),
        "environment_keys": sorted(helper_kwargs["env"]),
        "step_cap_config": helper_kwargs["env"].get("OPENCODE_CONFIG_CONTENT"),
        "timeout_seconds": helper_kwargs["timeout"],
        "agent_file_bytes": None,
    } == _snapshot("helper.json")

    monkeypatch.setattr(
        evaluator_module.os,
        "environ",
        {
            "BASE_ENV": "ignored",
            "MAGNOLIA_OPENCODE_V2_BIN": str(tmp_path / "opencode"),
        },
    )
    monkeypatch.setattr(evaluator_module.llm, "is_llm_available", lambda: True)
    decision = evaluator_module.GenerativeEvaluator(
        "opencode-go/deepseek-v4.1-flash",
        executable_available=lambda _path: True,
    ).evaluate("dock", {"projects": ["proj_a"]})
    assert decision is not None
    assert evaluator_module._catalog_has_model("jev-1.13-free") is True
    evaluator_run = subprocess_calls[-2]
    evaluator_catalog = subprocess_calls[-1]
    assert {
        "run_argv": _normalise(evaluator_run[0], tmp_path),
        "catalog_argv": _normalise(evaluator_catalog[0], tmp_path),
        "environment_keys": sorted(evaluator_run[1]["env"]),
        "step_cap_config": evaluator_run[1]["env"].get(
            "OPENCODE_CONFIG_CONTENT"
        ),
        "run_timeout_seconds": evaluator_run[1]["timeout"],
        "catalog_timeout_seconds": evaluator_catalog[1]["timeout"],
        "agent_file_bytes": None,
    } == _snapshot("evaluator.json")


def test_governor_cap_reaches_service_and_each_recipe(tmp_path, monkeypatch):
    from campaign_runner import workers as workers_module
    from campaign_runner.executors.opencode import OpenCodeServiceExecutor

    contexts = {}
    for role, role_cap, agent in (
        ("worker", 30, "campaign-worker"),
        ("reviewer", 8, "campaign-reviewer"),
        ("helper", 40, "build"),
        ("evaluator", 1, "build"),
    ):
        route = _route(role)
        context = ExecContext(
            route=route,
            role=role,
            role_cap=role_cap,
            governor_cap=6,
            opencode_binary="opencode",
            agent=agent,
        )
        contexts[role] = executor_for(route, role, context)

    assert contexts["worker"].max_steps == 6
    assert contexts["reviewer"].max_steps == 6
    helper_config = json.loads(
        contexts["helper"].environment_with_cap({})["OPENCODE_CONFIG_CONTENT"]
    )
    evaluator_config = json.loads(
        contexts["evaluator"].environment_with_cap({})["OPENCODE_CONFIG_CONTENT"]
    )
    assert helper_config["agents"]["build"]["steps"] == 6
    assert evaluator_config["agents"]["build"]["steps"] == 1

    service = OpenCodeServiceExecutor(
        binary="opencode", db_path="db", project_dir=tmp_path, governor_cap=6
    )
    config = service.config_with_caps(
        json.dumps({"agents": {
            "campaign-worker": {"steps": 30},
            "campaign-reviewer": {"steps": 8},
        }})
    )
    assert {
        name: value["steps"] for name, value in json.loads(config)["agents"].items()
    } == {"campaign-worker": 6, "campaign-reviewer": 6}

    route = _route("worker")
    missing = ExecContext(
        route=route,
        role="worker",
        project_dir=tmp_path,
        role_cap=30,
        governor_cap=6,
        opencode_server="http://external.invalid:4111",
        agent="campaign-worker",
    )
    with pytest.raises(ExecutorRefused, match="opencode_service_caps"):
        executor_for(route, "worker", missing)
    compatible = ExecContext(
        **{
            **missing.__dict__,
            "setup": {"opencode_service_caps": {"campaign-worker": 6}},
        }
    )
    executor = executor_for(route, "worker", compatible)
    launches = []

    def fake_run_process(**kwargs):
        launches.append(kwargs["argv"])
        kwargs["stdout_path"].write_text(
            json.dumps({
                "sessionID": "ses_cap",
                "part": {"type": "text", "messageID": "m1", "text": "DONE"},
            }) + "\n"
        )
        kwargs["stderr_path"].write_text("")
        return ProcessOutcome(
            0, False, 0.1, kwargs["stdout_path"], kwargs["stderr_path"]
        )

    monkeypatch.setattr(workers_module, "run_process", fake_run_process)
    executor.run("WORK", tmp_path, tmp_path / "compatible")
    compatible.setup["opencode_service_caps"]["campaign-worker"] = 7
    with pytest.raises(ExecutorRefused, match="campaign-worker"):
        executor.run("WORK", tmp_path, tmp_path / "incompatible")
    assert len(launches) == 1


def _quota_binary(tmp_path, text):
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = tmp_path / "opencode"
    body = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'sessionID':'ses_q','part':{'type':'step-start'}}))\n"
    )
    if text:
        event = {
            "sessionID": "ses_q",
            "part": {"type": "text", "messageID": "m1", "text": text},
        }
        body += f"print(json.dumps({event!r}))\n"
    binary.write_text(body, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_quota_only_suppresses_ordinary_retries(tmp_path):
    quota = RunOutcome(
        "quota", LimitInfo("subscription quota", None, "recorded output")
    )
    control = ProcessControl(observe=lambda stdout, stderr: quota)
    worker = OpenCodeExecutor(
        str(_quota_binary(tmp_path / "worker", "")),
        "provider/model",
        "campaign-worker",
    )
    worker_result = worker.run(
        "WORK", tmp_path, tmp_path / "worker-attempt", control=control
    )
    assert worker_result.attempts == 1
    assert worker_result.outcome is not None
    assert worker_result.outcome.kind == "quota"

    reviewer = Reviewer(
        binary=str(_quota_binary(tmp_path / "reviewer", "not json")),
        max_retries=2,
    )
    review_result = reviewer.run(
        "provider/model",
        "REVIEW",
        tmp_path,
        tmp_path / "review-attempt",
        tmp_path,
        control=control,
    )
    assert review_result.attempts == 1
    assert review_result.outcome is not None
    assert review_result.outcome.kind == "quota"


def _stopped_barrier_probe(result_queue, pid_path, tmp_path, stop_reason):
    binary = Path(tmp_path) / "client"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    def stop_launcher(pid, _pgid):
        Path(pid_path).write_text(str(pid), encoding="utf-8")
        os.kill(pid, signal.SIGSTOP)

    outcome = run_process(
        [str(binary)],
        cwd=tmp_path,
        env={"BIG": "x" * (512 * 1024)},
        timeout_seconds=0.2 if stop_reason is None else 30,
        stdout_path=Path(tmp_path) / "out.log",
        stderr_path=Path(tmp_path) / "err.log",
        control=ProcessControl(
            on_start=stop_launcher,
            should_stop=(lambda: stop_reason),
            poll_interval=0.01,
            hold_before_exec=True,
        ),
    )
    result_queue.put((outcome.timed_out, outcome.stopped_reason, outcome.outcome.kind))


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [(None, (True, None, "timeout")), ("operator stop", (False, "operator stop", "cancelled"))],
)
def test_stopped_launcher_payload_write_obeys_deadline_and_stop(
    tmp_path, stop_reason, expected
):
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    pid_path = tmp_path / "launcher.pid"
    child = context.Process(
        target=_stopped_barrier_probe,
        args=(result_queue, pid_path, str(tmp_path), stop_reason),
    )
    child.start()
    child.join(2)
    launcher_pid = int(pid_path.read_text()) if pid_path.exists() else None
    if child.is_alive():
        child.terminate()
        child.join(2)
        if launcher_pid is not None:
            try:
                os.killpg(launcher_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        pytest.fail("governed environment write ignored the invocation boundary")
    assert child.exitcode == 0
    assert result_queue.get(timeout=1) == expected
    if launcher_pid is not None:
        for _ in range(50):
            try:
                os.kill(launcher_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"stopped launcher {launcher_pid} was not reaped")


@pytest.mark.parametrize(
    ("stop_reason", "expected_kind", "expected_timeout"),
    [
        (None, "timeout", True),
        ("operator stop", "cancelled", False),
    ],
)
def test_pre_exec_interruption_releases_reservation_without_receipt(
    tmp_path, stop_reason, expected_kind, expected_timeout
):
    from campaign_runner.governor import Governor, Policy
    from campaign_runner.workers import InvocationGovernance

    binary = tmp_path / "client"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    ledger = tmp_path / "usage-ledger.jsonl"
    governor = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=ledger,
    )

    launcher_started = False

    def stop_launcher(pid, _pgid):
        nonlocal launcher_started
        os.kill(pid, signal.SIGSTOP)
        launcher_started = True

    def stop_after_launcher_started():
        return stop_reason if launcher_started else None

    executor = OpenCodeExecutor(
        binary=str(binary),
        model="provider/model",
        agent="campaign-worker",
        environment={"BIG": "x" * (512 * 1024)},
        timeout_seconds=0.2 if stop_reason is None else 30,
        governance=InvocationGovernance(
            governor=governor,
            route=_route("worker"),
            parent_run_id="pre-exec-run",
            project_digest="project",
        ),
    )
    result = executor.run(
        "WORK",
        tmp_path,
        tmp_path / "attempt",
        control=ProcessControl(
            on_start=stop_launcher,
            should_stop=stop_after_launcher_started,
            poll_interval=0.01,
        ),
    )

    assert launcher_started
    assert result.attempts == 1
    assert result.outcome is not None
    assert result.outcome.kind == expected_kind
    assert result.timed_out is expected_timeout
    assert result.stopped_reason == stop_reason
    assert not ledger.exists() or not ledger.read_text()
    state = json.loads(governor.state_path.read_text())
    assert state["reservations"] == {}
    assert state["completed"] == {}


def test_payload_write_supports_fd_above_select_limit(tmp_path, monkeypatch):
    from campaign_runner import process as process_module

    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit <= 1024:
        pytest.skip(f"open-file soft limit {soft_limit} cannot allocate fd >= 1024")

    read_fd, write_fd = os.pipe()
    high_fd = None
    try:
        try:
            high_fd = fcntl.fcntl(write_fd, fcntl.F_DUPFD, 1024)
        except OSError as exc:
            pytest.skip(f"platform cannot duplicate a write fd above 1024: {exc}")
        os.close(write_fd)
        write_fd = None
        if high_fd < 1024:
            pytest.skip(f"duplicated fd {high_fd} is below 1024")

        real_write = os.write
        blocked_once = False

        def block_then_write(fd, body):
            nonlocal blocked_once
            if fd == high_fd and not blocked_once:
                blocked_once = True
                raise BlockingIOError
            return real_write(fd, body)

        monkeypatch.setattr(process_module.os, "write", block_then_write)
        process_module._write_all(
            high_fd,
            b"payload",
            deadline=time.monotonic() + 1,
            control=ProcessControl(poll_interval=0.01),
        )
        assert blocked_once
        assert os.read(read_fd, len(b"payload")) == b"payload"
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if high_fd is not None:
            os.close(high_fd)


def test_legacy_managed_service_without_caps_requests_idle_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "campaign_runner.executors.registry.read_service",
        lambda project: {
            "port": 4111,
            "running": True,
            "project_path": str(tmp_path.resolve()),
        },
    )
    monkeypatch.setattr(
        "campaign_runner.executors.registry.service_status",
        lambda project: {"running": True},
    )
    route = _route("worker")
    context = ExecContext(
        route=route,
        role="worker",
        project_dir=tmp_path,
        role_cap=30,
        governor_cap=6,
        opencode_server="http://127.0.0.1:4111",
        agent="campaign-worker",
    )

    with pytest.raises(
        ExecutorRefused,
        match="service started without recorded caps — restart it when idle",
    ):
        executor_for(route, "worker", context)


def test_managed_service_with_excess_caps_requests_idle_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "campaign_runner.executors.registry.read_service",
        lambda project: {
            "port": 4111,
            "running": True,
            "project_path": str(tmp_path.resolve()),
            "agent_steps": {"campaign-worker": 30},
        },
    )
    monkeypatch.setattr(
        "campaign_runner.executors.registry.service_status",
        lambda project: {"running": True},
    )
    route = _route("worker")
    context = ExecContext(
        route=route,
        role="worker",
        project_dir=tmp_path,
        role_cap=30,
        governor_cap=6,
        opencode_server="http://127.0.0.1:4111",
        agent="campaign-worker",
    )

    with pytest.raises(
        ExecutorRefused,
        match="managed OpenCode service caps exceed the governor cap — restart it when idle",
    ):
        executor_for(route, "worker", context)
