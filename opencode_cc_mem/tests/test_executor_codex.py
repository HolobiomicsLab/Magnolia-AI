"""Task 8 contract tests for the governed Codex executor."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.codex import CodexExecutor, parse_codex_stream
from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.governor import Governor, Policy
from campaign_runner.launch import configuration_identity
from campaign_runner.routing import ResolvedRoute
from campaign_runner.workers import EMPTY_OUTPUT_NOTE, InvocationGovernance
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_codex import native_artifacts


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "executors" / "codex" / "live-p3"
FORBIDDEN_ENV = {
    "CLAUDE_CONFIG_DIR": "forbidden-claude-config",
    "CODEX_HOME": "forbidden-codex-home",
    "VIBE_HOME": "forbidden-vibe-home",
    "CODEX_API_KEY": "forbidden-codex-key",
    "ANTHROPIC_API_KEY": "forbidden-anthropic-key",
    "ANTHROPIC_AUTH_TOKEN": "forbidden-anthropic-token",
}


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=max(seconds, 0.001))


def _stream(run: str) -> str:
    return "\n".join(
        json.dumps(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((FIXTURES / run).glob("*-stream-*.json"))
    ) + "\n"


def _jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def _stub(
    tmp_path: Path,
    streams: list[Path],
    *,
    returncodes: list[int] | None = None,
    linger_after_first: bool = False,
) -> tuple[Path, Path, Path]:
    state = tmp_path / "stub-state"
    state.mkdir()
    config = tmp_path / "codex-stub.json"
    config.write_text(json.dumps({
        "streams": [str(path) for path in streams],
        "returncodes": returncodes or [0] * len(streams),
        "state": str(state),
        "linger_after_first": linger_after_first,
    }))
    binary = tmp_path / "codex-stub"
    binary.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys, time
config = json.loads(pathlib.Path(os.environ["CODEX_STUB_CONFIG"]).read_text())
state = pathlib.Path(config["state"])
index = len(list(state.glob("call-*.json")))
(state / f"call-{index}.json").write_text(json.dumps({
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "forbidden_env": {
        name: os.environ.get(name)
        for name in (
            "CLAUDE_CONFIG_DIR", "CODEX_HOME", "VIBE_HOME", "CODEX_API_KEY",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        )
        if name in os.environ
    },
    "role": os.environ.get("MAGNOLIA_ROLE"),
}))
sys.stdout.write(pathlib.Path(config["streams"][index]).read_text())
sys.stdout.flush()
if index == 0 and config["linger_after_first"]:
    time.sleep(5)
    (state / "was-not-terminated").write_text("late")
raise SystemExit(config["returncodes"][index])
"""
    )
    binary.chmod(0o755)
    return binary, config, state


def _event_name(native: str) -> str:
    return native[0].lower() + native[1:]


def _context(
    tmp_path: Path,
    role: str = "helper",
    model: str = "gpt-test",
    *,
    binary: Path | None = None,
    stub_config: Path | None = None,
    governance: InvocationGovernance | None = None,
) -> ExecContext:
    project = tmp_path / "project"
    run = tmp_path / "run"
    (project / ".codex").mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (run / "work").mkdir()
    launch_context = LaunchContext(
        root=ROOT, project="project-fixture", project_dir=project,
        python="/venv/bin/python", tools_port=8123, model=model, setup={},
        run_dir=run, control_dir=run / "control", work_dir=run / "work",
        role=role, invocation_id=f"{role}-one", noninteractive=True,
    )
    for relative, body in native_artifacts(launch_context, {}).items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    hooks_path = project / ".codex" / "hooks.json"
    hooks = json.loads(hooks_path.read_text())["hooks"]
    rows = []
    states = []
    for native_event, groups in hooks.items():
        for group_index, group in enumerate(groups):
            for hook_index, _handler in enumerate(group["hooks"]):
                snake = "".join(
                    ("_" + char.lower()) if char.isupper() else char
                    for char in native_event
                ).lstrip("_")
                key = f"{hooks_path.resolve()}:{snake}:{group_index}:{hook_index}"
                digest = f"sha256:{native_event.lower()}-{group_index}-{hook_index}"
                rows.append({
                    "key": key,
                    "currentHash": digest,
                    "trustStatus": "trusted",
                    "enabled": True,
                    "eventName": _event_name(native_event),
                    "sourcePath": str(hooks_path.resolve()),
                })
                states.append((key, digest))
    trust = tmp_path / "codex-user.toml"
    trust_body = [
        f'[projects.{json.dumps(str(project.resolve()))}]',
        'trust_level = "trusted"',
        "",
        '[mcp_servers.user_mail]',
        'command = "mail-server"',
        "",
        '[plugins.user_plugin]',
        'enabled = true',
        "",
    ]
    for key, digest in states:
        trust_body.extend([
            f'[hooks.state.{json.dumps(key)}]',
            f'trusted_hash = {json.dumps(digest)}',
            "",
        ])
    trust.write_text("\n".join(trust_body))
    route = ResolvedRoute("codex", model, "family", "codex", role, "test")
    evidence = {
        "accepted": True, "trusted": True, "stale": False, "identity": "pending",
        "discovery_dir": str(project.resolve()), "capabilities": [],
        "model_isolation": {"accepted": True, "model": model, "identity": "fixture"},
    }
    environment = {"PATH": os.environ.get("PATH", ""), **FORBIDDEN_ENV}
    if stub_config is not None:
        environment["CODEX_STUB_CONFIG"] = str(stub_config)
    context = ExecContext(
        route=route, role=role, project_dir=project, run_dir=run,
        control_dir=run / "control", work_dir=run / "work",
        installation_root=ROOT, workspace_root=tmp_path,
        project_identity="project-fixture", python_executable="/venv/bin/python",
        tools_port=8123, launch_argv=(str(binary or "codex-stub"),),
        memory_service={"transport": "http", "url": "http://127.0.0.1:8765/mcp"},
        role_capabilities={"codex": {role: evidence}},
        environment=environment,
        setup={
            "codex_user_config_path": str(trust),
            "codex_hooks_list": rows,
        },
        governance=governance,
        timeout_seconds=3,
        invocation_id=f"{role}-one",
    )
    evidence["identity"] = configuration_identity(context)
    return context


def _calls(state: Path) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in sorted(state.glob("call-*.json"))
    ]


def _governance(tmp_path: Path, role: str) -> InvocationGovernance:
    clock = FakeClock()
    governor = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
        sleeper=clock.sleep,
    )
    route = ResolvedRoute("codex", "gpt-test", "family", "codex", role, "test")
    return InvocationGovernance(
        governor, route, "parent-run", "project-fixture", role_cap=1
    )


@pytest.mark.parametrize("role", ("worker", "reviewer"))
def test_codex_worker_and_reviewer_full_gate_or_refusal(tmp_path, role):
    context = _context(tmp_path, role=role)
    with pytest.raises(ExecutorRefused, match="D2"):
        executor_for(context.route, role, context)


@pytest.mark.parametrize("role", ("helper", "evaluator"))
def test_codex_helper_evaluator_independent_contracts(tmp_path, role):
    context = _context(tmp_path, role=role)
    executor = executor_for(context.route, role, context)
    assert isinstance(executor, CodexExecutor)
    argv = executor._argv("prompt", None)
    assert argv[argv.index("-m") + 1] == "gpt-test"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(context.project_dir)
    pairs = [argv[index:index + 2] for index in range(len(argv))]
    assert ["--disable", "code_mode_host"] in pairs
    assert ["--disable", "apps"] in pairs
    assert 'mcp_servers."user_mail".enabled=false' in argv
    assert 'plugins."user_plugin".enabled=false' in argv


def test_codex_recorded_stream_cache_and_resume_identity():
    first = parse_codex_stream(_stream("x1"))
    resumed = parse_codex_stream(_stream("x4b"), baseline=first.usage)
    assert first.session_id == "ses-fixture-1"
    assert first.final_text.startswith("1. BLOCKED")
    assert first.usage.cached_tokens == 123648
    assert resumed.usage.input_tokens == 426576 - 138332
    assert resumed.usage.cached_tokens == 387968 - 123648
    assert resumed.cumulative_usage.input_tokens == 426576


def test_codex_parser_survives_garbage():
    parsed = parse_codex_stream(
        'garbage\n{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}\n{'
    )
    assert parsed.final_text == "partial"
    assert [item["kind"] for item in parsed.diagnostics] == ["invalid_json", "invalid_json"]


def test_codex_x7_evaluator_cap_one_succeeds_through_run(tmp_path):
    stream = tmp_path / "x7.jsonl"
    stream.write_text(_stream("x7"))
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt", retries=0
    )

    assert result.outcome.kind == "success"
    assert result.final_text == "EVAL-OK 4"
    assert result.attempts == 1
    (call,) = _calls(state)
    assert call["forbidden_env"] == {}
    assert call["role"] == "evaluator"


def test_codex_cap_stops_lingering_process_group(tmp_path):
    stream = _jsonl(tmp_path / "over-cap.jsonl", [
        {"type": "thread.started", "thread_id": "cap-thread"},
        {"type": "turn.completed", "usage": {}},
        {"type": "turn.completed", "usage": {}},
    ])
    binary, config, state = _stub(tmp_path, [stream], linger_after_first=True)
    context = _context(
        tmp_path,
        role="evaluator",
        binary=binary,
        stub_config=config,
        governance=_governance(tmp_path, "evaluator"),
    )

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "cap_reached"
    assert result.stopped_reason == "cap_reached"
    assert not (state / "was-not-terminated").exists()
    rows = (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["outcome"]["kind"] == "cap_reached"


def test_codex_normal_exit_over_cap_keeps_client_outcome(tmp_path):
    stream = _jsonl(tmp_path / "normal-over-cap.jsonl", [
        {"type": "thread.started", "thread_id": "normal-thread"},
        {"type": "turn.completed", "usage": {}},
        {"type": "turn.completed", "usage": {}},
    ])
    binary, config, _state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt", retries=0
    )

    assert result.outcome.kind == "success"
    assert result.stopped_reason is None


def test_client_resume_preserves_caps_and_tools_values(tmp_path):
    context = _context(tmp_path)
    executor = executor_for(context.route, "helper", context)
    executor._bind_session("thread-1", parse_codex_stream(_stream("x1")).usage)
    fresh = executor._argv("first prompt", None)
    resumed = executor._argv("retry prompt", "thread-1")

    fresh.remove("first prompt")
    resumed.remove("retry prompt")
    resumed.remove("resume")
    resumed.remove("thread-1")
    assert fresh == resumed


def test_codex_empty_retry_uses_two_reservations_with_same_restrictions(tmp_path):
    empty = _jsonl(tmp_path / "empty.jsonl", [
        {"type": "thread.started", "thread_id": "retry-thread"},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    recovered = _jsonl(tmp_path / "recovered.jsonl", [
        {"type": "thread.started", "thread_id": "retry-thread"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "recovered"}},
        {"type": "turn.completed", "usage": {"input_tokens": 15}},
    ])
    binary, config, state = _stub(tmp_path, [empty, recovered])
    governance = _governance(tmp_path, "evaluator")
    context = _context(
        tmp_path, role="evaluator", binary=binary, stub_config=config,
        governance=governance,
    )

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.final_text == "recovered"
    assert result.attempts == 2
    calls = _calls(state)
    assert len(calls) == 2
    first, resumed = (call["argv"].copy() for call in calls)
    first.remove("evaluate")
    resumed.remove(EMPTY_OUTPUT_NOTE)
    resumed.remove("resume")
    resumed.remove("retry-thread")
    assert first == resumed
    rows = [json.loads(line) for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[1]["session_identity"]["session_id"] == "retry-thread"


def test_codex_quota_precedes_success_empty_retry(tmp_path):
    stream = _jsonl(tmp_path / "quota-empty.jsonl", [
        {"type": "thread.started", "thread_id": "quota-thread"},
        {"type": "turn.completed", "synthetic_quota": True, "usage": {}},
    ])
    binary, config, state = _stub(tmp_path, [stream], linger_after_first=True)
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    quota = RunOutcome("quota", LimitInfo("fixture quota", None, "synthetic"))
    executor = executor_for(context.route, "evaluator", context)
    executor.quota_detector = (
        lambda event: quota if event.get("synthetic_quota") else None
    )

    result = executor.run("evaluate", context.project_dir, tmp_path / "attempt")

    assert result.outcome.kind == "quota"
    assert result.attempts == 1
    assert len(_calls(state)) == 1
    assert not (state / "was-not-terminated").exists()


@pytest.mark.parametrize("damage", ("hook", "mcp", "model"))
def test_codex_refuses_each_project_native_drift(tmp_path, damage):
    context = _context(tmp_path)
    if damage == "hook":
        path = context.project_dir / ".codex" / "hooks.json"
        payload = json.loads(path.read_text())
        payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "drifted"
        path.write_text(json.dumps(payload) + "\n")
    else:
        path = context.project_dir / ".codex" / "config.toml"
        suffix = (
            '\n[mcp_servers.drift]\ncommand = "drift"\n'
            if damage == "mcp"
            else '\nmodel = "drifted-model"\n'
        )
        path.write_text(path.read_text() + suffix)
    context.role_capabilities["codex"]["helper"]["identity"] = configuration_identity(context)

    with pytest.raises(ExecutorRefused, match="project.*Codex|Codex.*project"):
        executor_for(context.route, "helper", context)


@pytest.mark.parametrize("damage", ("missing_row", "missing_hash"))
def test_codex_required_hook_trust_fails_closed(tmp_path, damage):
    context = _context(tmp_path)
    rows = context.setup["codex_hooks_list"]
    pre_tool = next(row for row in rows if row["eventName"] == "preToolUse")
    if damage == "missing_row":
        rows.remove(pre_tool)
    else:
        trust = Path(context.setup["codex_user_config_path"])
        body = trust.read_text()
        marker = f'[hooks.state.{json.dumps(pre_tool["key"])}]\n'
        start = body.index(marker)
        value_start = body.index("trusted_hash", start)
        value_end = body.index("\n", value_start)
        trust.write_text(body[:value_start] + body[value_end + 1:])

    with pytest.raises(ExecutorRefused, match="/hooks|trust"):
        executor_for(context.route, "helper", context)


def test_codex_user_config_change_invalidates_capability_identity(tmp_path):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text() + '\n[mcp_servers.late_server]\ncommand = "late"\n'
    )

    with pytest.raises(ExecutorRefused, match="configuration identity"):
        executor_for(context.route, "helper", context)


def test_client_nonquota_errors_stay_failures():
    parsed = parse_codex_stream('{"type":"error","message":"ordinary"}')
    assert parsed.outcome is None
    assert parsed.result_seen is False
    assert parsed.diagnostics[0]["kind"] == "client_error"
