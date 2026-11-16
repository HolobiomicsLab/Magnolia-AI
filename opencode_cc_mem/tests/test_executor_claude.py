import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, executor_for
from campaign_runner.executors.claude_code import (
    ClaudeCodeExecutor,
    parse_claude_stream,
)
from campaign_runner.governor import Governor, Policy
from campaign_runner.launch import configuration_identity
from campaign_runner.routing import ResolvedRoute
from campaign_runner.workers import InvocationGovernance


ROOT = Path(__file__).parents[1]
LIVE = ROOT / "tests/fixtures/executors/claude/live-p3"
SYNTHETIC = ROOT / "tests/fixtures/executors/claude/synthetic"
FORBIDDEN_ENV = {
    "CLAUDE_CONFIG_DIR": "forbidden-config",
    "ANTHROPIC_API_KEY": "forbidden-key",
}
ROLE_CAPABILITIES = {
    "worker": (
        "native_read",
        "native_glob",
        "native_grep",
        "native_edit",
        "mcp:compchem-memory.memory_get_context",
        "mcp:compchem-memory.memory_search",
        "mcp:compchem-memory.memory_get_run_history",
    ),
    "reviewer": ("native_read", "native_glob", "native_grep"),
    "helper": (),
    "evaluator": (),
}


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=max(seconds, 0.001))


def _jsonl(path, events):
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _live_event(cell, name):
    return json.loads((LIVE / cell / name).read_text(encoding="utf-8"))


def _stub(tmp_path, streams, *, returncodes=None, linger_after_first=False):
    state = tmp_path / "stub-state"
    state.mkdir()
    config = tmp_path / "stub-config.json"
    config.write_text(
        json.dumps(
            {
                "streams": [str(path) for path in streams],
                "returncodes": returncodes or [0] * len(streams),
                "state": str(state),
                "linger_after_first": linger_after_first,
            }
        ),
        encoding="utf-8",
    )
    binary = tmp_path / "claude-stub"
    binary.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys, time
config = json.loads(pathlib.Path(os.environ["CLAUDE_STUB_CONFIG"]).read_text())
state = pathlib.Path(config["state"])
index = len(list(state.glob("call-*.json")))
(state / f"call-{index}.json").write_text(json.dumps({
    "argv": sys.argv,
    "forbidden_env": {
        name: os.environ.get(name)
        for name in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY")
        if name in os.environ
    },
    "role": os.environ.get("MAGNOLIA_ROLE"),
    "policy": os.environ.get("MAGNOLIA_HOOK_POLICY"),
}))
body = pathlib.Path(config["streams"][index]).read_text()
sys.stdout.write(body)
sys.stdout.flush()
if index == 0 and config["linger_after_first"]:
    time.sleep(5)
    (state / "was-not-terminated").write_text("late")
raise SystemExit(config["returncodes"][index])
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, config, state


def _context(
    tmp_path,
    binary,
    config,
    *,
    role="worker",
    role_cap=30,
    governor_cap=8,
    governance=None,
    invocation="claude-one",
):
    project = tmp_path / "project"
    run = tmp_path / "run"
    project.mkdir(exist_ok=True)
    (run / "control").mkdir(parents=True, exist_ok=True)
    (run / "work").mkdir(parents=True, exist_ok=True)
    route = ResolvedRoute(
        "claude-code", "haiku", "claude-haiku", "claude-code", role, "test"
    )
    capabilities = ROLE_CAPABILITIES[role]
    evidence = {
        "accepted": True,
        "trusted": True,
        "stale": False,
        "identity": "pending",
        "discovery_dir": str(project.resolve()),
        "capabilities": list(capabilities),
        "model_isolation": {
            "accepted": True,
            "model": "haiku",
            "identity": "claude-model-flag-v1",
        },
    }
    values = {
        "route": route,
        "role": role,
        "installation_root": ROOT,
        "workspace_root": tmp_path,
        "project_identity": "project-digest",
        "project_dir": project,
        "run_dir": run,
        "control_dir": run / "control",
        "work_dir": run / "work",
        "python_executable": os.environ.get("PYTHON", "/usr/bin/python3"),
        "tools_port": 8123,
        "setup": {"memory_enabled": True},
        "memory_service": {
            "transport": "http",
            "url": "http://127.0.0.1:8765/mcp",
        },
        "invocation_id": invocation,
        "launch_argv": (str(binary),),
        "role_capabilities": {"claude-code": {role: evidence}},
        "environment": {
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_STUB_CONFIG": str(config),
            **FORBIDDEN_ENV,
        },
        "role_cap": role_cap,
        "governor_cap": governor_cap,
        "governance": governance,
        "timeout_seconds": 3,
    }
    context = ExecContext(**values)
    evidence["identity"] = configuration_identity(context)
    return context


def _calls(state):
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(state.glob("call-*.json"))
    ]


def _synthetic_quota_event(*, partial="", reset_at=None):
    fixture = json.loads((SYNTHETIC / "quota.json").read_text(encoding="utf-8"))
    event = dict(fixture["event"])
    event["synthetic"] = fixture["synthetic"]
    event["result"] = partial or event["result"]
    if reset_at is not None:
        event["reset_at"] = reset_at
    return event


def test_claude_effective_argv_env_model_and_cap(tmp_path):
    stream = _jsonl(
        tmp_path / "success.jsonl",
        [_live_event("c1", "17-stream-init.json"), _live_event("c1", "24-stream-result.json")],
    )
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="worker", role_cap=30)

    executor = executor_for(context.route, "worker", context)
    result = executor.run("work", context.project_dir, tmp_path / "attempt")

    assert isinstance(executor, ClaudeCodeExecutor)
    assert "ALLOWED-README" in result.final_text
    (call,) = _calls(state)
    argv = call["argv"]
    assert argv[0] == str(binary)
    assert argv[1:3] == ["-p", "work"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--max-turns") + 1] == "8"
    assert argv[argv.index("--settings") + 1].endswith(
        "/control/executors/claude-one/settings.json"
    )
    assert argv[argv.index("--mcp-config") + 1].endswith("/control/executors/claude-one/mcp.json")
    assert "--strict-mcp-config" in argv
    assert ["--output-format", "stream-json"] == argv[
        argv.index("--output-format") : argv.index("--output-format") + 2
    ]
    assert "--verbose" in argv
    assert "--tools" not in argv
    allowed = argv[argv.index("--allowedTools") + 1 :]
    assert allowed == [
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "mcp__compchem-memory__memory_get_context",
        "mcp__compchem-memory__memory_search",
        "mcp__compchem-memory__memory_get_run_history",
    ]
    mcp = json.loads(
        Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8")
    )
    assert mcp["tool_allowlist"] == list(ROLE_CAPABILITIES["worker"][-3:])
    assert call["forbidden_env"] == {}
    assert call["role"] == "worker"
    assert Path(call["policy"]).is_absolute()


def test_claude_evaluator_strict_config_removes_all_tools(tmp_path):
    stream = _jsonl(
        tmp_path / "success.jsonl",
        [_live_event("c7", "06-stream-init.json"), _live_event("c7", "07-stream-result.json")],
    )
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.final_text == "EVAL-OK 4"
    (call,) = _calls(state)
    argv = call["argv"]
    assert argv[argv.index("--tools") :] == ["--tools", ""]
    mcp = json.loads(
        Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8")
    )
    assert mcp["memory_service"] is None
    assert mcp["tool_allowlist"] == []


def test_claude_recorded_stream_usage_and_cache():
    text = "\n".join(
        json.dumps(json.loads((LIVE / "c1" / name).read_text(encoding="utf-8")))
        for name in (
            "17-stream-init.json",
            "18-stream-tool-result-with-permission-meta.json",
            "19-stream-tool-result-with-permission-meta.json",
            "24-stream-result.json",
        )
    )

    parsed = parse_claude_stream(text)

    assert parsed.session_id == "ses-fixture-1"
    assert parsed.reported_model == "claude-haiku-5-5"
    assert parsed.tool_events == 2
    assert parsed.usage.input_tokens == 16
    assert parsed.usage.output_tokens == 1934
    assert parsed.usage.reasoning_tokens == 719
    assert parsed.usage.cached_tokens == 164374
    assert parsed.usage.cache_write_tokens == 9912
    assert parsed.usage.turns == 8
    assert parsed.model_usage["claude-haiku-5-5"]["cacheReadInputTokens"] == 164374
    assert parsed.terminal_reason == "completed"
    assert parsed.is_error is False


def test_claude_parser_survives_garbage():
    assistant = {
        "type": "assistant",
        "session_id": "partial-session",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "partial reply"},
                {"type": "tool_use", "name": "Read", "id": "tool-1", "input": {}},
            ],
        },
    }
    text = json.dumps(assistant) + "\nnot-json\n{\"type\":\"result\""

    parsed = parse_claude_stream(text)

    assert parsed.final_text == "partial reply"
    assert parsed.all_text == "partial reply"
    assert parsed.tool_events == 1
    assert parsed.usage.input_tokens is None
    assert parsed.usage.cached_tokens is None
    assert parsed.result_seen is False
    assert len(parsed.diagnostics) == 2


def test_claude_one_same_session_empty_retry(tmp_path):
    empty = _jsonl(
        tmp_path / "empty.jsonl",
        [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "retry-session",
                "model": "claude-haiku-5-5",
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "retry-session",
                "result": "",
                "is_error": False,
                "terminal_reason": "completed",
                "num_turns": 1,
            },
        ],
    )
    recovered = _jsonl(
        tmp_path / "recovered.jsonl",
        [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "retry-session",
                "model": "claude-haiku-5-5",
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "retry-session",
                "result": "recovered",
                "is_error": False,
                "terminal_reason": "completed",
                "num_turns": 1,
            },
        ],
    )
    binary, config, state = _stub(tmp_path, [empty, recovered])
    clock = FakeClock()
    governor = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
        sleeper=clock.sleep,
    )
    route = ResolvedRoute(
        "claude-code", "haiku", "claude-haiku", "claude-code", "evaluator", "test"
    )
    governance = InvocationGovernance(
        governor, route, "parent-run", "project-digest", role_cap=1
    )
    context = _context(
        tmp_path,
        binary,
        config,
        role="evaluator",
        role_cap=1,
        governance=governance,
    )

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.final_text == "recovered"
    assert result.attempts == 2
    calls = _calls(state)
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == "retry-session"
    for flag in ("--settings", "--mcp-config", "--strict-mcp-config", "--model", "--tools"):
        assert calls[0]["argv"].count(flag) == calls[1]["argv"].count(flag) == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 2
    assert rows[0]["session_identity"] is None
    assert rows[1]["session_identity"]["session_id"] == "retry-session"


def test_claude_empty_retry_refuses_changed_session_identity(tmp_path):
    first = _jsonl(
        tmp_path / "first.jsonl",
        [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "expected-session",
                "model": "claude-haiku-5-5",
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "expected-session",
                "result": "",
                "is_error": False,
            },
        ],
    )
    changed = _jsonl(
        tmp_path / "changed.jsonl",
        [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "different-session",
                "model": "claude-haiku-5-5",
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "different-session",
                "result": "wrong continuation",
                "is_error": False,
            },
        ],
    )
    binary, config, _ = _stub(tmp_path, [first, changed])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "refused"
    assert result.attempts == 2
    assert result.errors[-1] == {
        "kind": "resume_identity_mismatch",
        "expected": "expected-session",
        "observed": "different-session",
    }


@pytest.mark.parametrize("returncode", (0, 1))
@pytest.mark.parametrize("partial", ("", "partial before limit"))
def test_claude_quota_precedes_empty_and_partial_reply(tmp_path, returncode, partial):
    stream = _jsonl(tmp_path / "quota.jsonl", [_synthetic_quota_event(partial=partial)])
    binary, config, state = _stub(tmp_path, [stream], returncodes=[returncode])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "quota"
    assert result.outcome.limit.evidence_source == "synthetic Claude fixture; not live-certified"
    assert result.attempts == 1
    assert len(_calls(state)) == 1
    if partial:
        assert result.final_text == partial


def test_claude_stream_limit_terminates_before_retry(tmp_path):
    reset = "2026-11-17T00:00:00+00:00"
    stream = _jsonl(
        tmp_path / "quota.jsonl",
        [_synthetic_quota_event(reset_at=reset)],
    )
    binary, config, state = _stub(
        tmp_path, [stream], linger_after_first=True
    )
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "quota"
    assert result.outcome.limit.reset_at.isoformat() == reset
    assert result.attempts == 1
    assert not (state / "was-not-terminated").exists()


@pytest.mark.parametrize(
    "message",
    (
        "Authentication failed: please login",
        "Unknown model claude-imaginary",
        "Tool Read is unavailable",
    ),
)
def test_claude_auth_model_tool_errors_are_not_quota(tmp_path, message):
    stream = _jsonl(
        tmp_path / "failure.jsonl",
        [
            {
                "type": "result",
                "subtype": "error",
                "result": message,
                "is_error": True,
                "terminal_reason": "error",
            }
        ],
    )
    binary, config, _ = _stub(tmp_path, [stream], returncodes=[1])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "failure"
    assert result.attempts == 1
