import json
import multiprocessing
import os
import signal
import time
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.process import ProcessControl, ProcessOutcome, run_process
from campaign_runner.reviewers import Reviewer
from campaign_runner.routing import resolve_route
from campaign_runner.workers import OpenCodeExecutor


def _route(role):
    inputs = {
        "main_model": "provider/model",
        "reviewer": {"model": "provider/model"},
        "agent": {"json": {"model": "provider/model"}},
        "model": "provider/model",
    }
    return resolve_route(role, inputs)


def test_opencode_recipes_unchanged_through_registry(tmp_path):
    from campaign_runner.executors.opencode import (
        OpenCodeEvaluatorExecutor,
        OpenCodeHelperExecutor,
        OpenCodeServiceExecutor,
    )

    worker = executor_for(
        _route("worker"),
        "worker",
        ExecContext(
            route=_route("worker"),
            role="worker",
            opencode_binary="opencode",
            opencode_server="http://127.0.0.1:4111",
            opencode_db="db",
            agent="campaign-worker",
        ),
    )
    reviewer = executor_for(
        _route("reviewer"),
        "reviewer",
        ExecContext(
            route=_route("reviewer"),
            role="reviewer",
            opencode_binary="opencode",
            agent="campaign-reviewer",
        ),
    )
    helper = executor_for(
        _route("helper"),
        "helper",
        ExecContext(route=_route("helper"), role="helper", opencode_binary="opencode"),
    )
    evaluator = executor_for(
        _route("evaluator"),
        "evaluator",
        ExecContext(route=_route("evaluator"), role="evaluator", opencode_binary="opencode"),
    )
    service = executor_for(
        _route("worker"),
        "service",
        ExecContext(route=_route("worker"), role="service", opencode_binary="opencode"),
    )

    assert isinstance(worker, OpenCodeExecutor)
    assert worker._argv("PROMPT", None) == [
        "opencode", "run", "--model", "provider/model", "--agent",
        "campaign-worker", "--format", "json", "--auto", "--server",
        "http://127.0.0.1:4111", "PROMPT",
    ]
    assert isinstance(reviewer, OpenCodeExecutor)
    assert reviewer.max_format_retries == 0
    assert reviewer._argv("REVIEW", "ses") == [
        "opencode", "run", "--model", "provider/model", "--agent",
        "campaign-reviewer", "--format", "json", "--auto", "--session",
        "ses", "REVIEW",
    ]
    assert isinstance(helper, OpenCodeHelperExecutor)
    assert helper.argv(["PROMPT"]) == [
        "opencode", "run", "--model", "provider/model", "--format", "json", "PROMPT",
    ]
    assert isinstance(evaluator, OpenCodeEvaluatorExecutor)
    assert evaluator.run_argv("PROMPT") == [
        "opencode", "--standalone", "run", "--model", "provider/model", "PROMPT",
    ]
    assert evaluator.catalog_argv() == [
        "opencode", "--standalone", "api", "get", "/api/model",
    ]
    assert isinstance(service, OpenCodeServiceExecutor)
    assert service.argv(4137) == [
        "opencode", "serve", "--hostname", "127.0.0.1", "--port", "4137",
    ]


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
