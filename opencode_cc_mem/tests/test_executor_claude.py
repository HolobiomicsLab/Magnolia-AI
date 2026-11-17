import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from campaign_runner.executors import (
    ExecContext,
    ExecutorRefused,
    LimitInfo,
    RunOutcome,
    executor_for,
)
from campaign_runner.executors.claude_code import (
    ClaudeCodeExecutor,
    parse_claude_stream,
)
from campaign_runner.governor import AdmissionRequest, Governor, Policy
from campaign_runner.launch import configuration_identity
from campaign_runner.process import ProcessControl
from campaign_runner.routing import ResolvedRoute
from campaign_runner.workers import InvocationGovernance, WorkerError


ROOT = Path(__file__).parents[1]
ISOLATED_MEMORY_ARGS = [
    "-I",
    str(
        ROOT / "mcp-servers" / "compchem-memory" / "src"
        / "compchem_memory" / "hook" / "runner.py"
    ),
    "--memory-server",
]
WORKER_MEMORY_ARGS = [
    *ISOLATED_MEMORY_ARGS,
    "--tool-allowlist-json",
    json.dumps(
        [
            "mcp:compchem-memory.memory_get_context",
            "mcp:compchem-memory.memory_search",
            "mcp:compchem-memory.memory_get_run_history",
        ],
        separators=(",", ":"),
    ),
]
LIVE = ROOT / "tests/fixtures/executors/claude/live-p3"
SYNTHETIC = ROOT / "tests/fixtures/executors/claude/synthetic"
STRIPPED_ENV = {
    "CLAUDE_CONFIG_DIR": "forbidden-config",
    "CODEX_HOME": "forbidden-codex-home",
    "VIBE_HOME": "forbidden-vibe-home",
    "CODEX_API_KEY": "forbidden-codex-key",
    "ANTHROPIC_API_KEY": "forbidden-key",
    "ANTHROPIC_AUTH_TOKEN": "forbidden-anthropic-token",
    "OPENAI_API_KEY": "forbidden-openai-key",
    "MISTRAL_API_KEY": "forbidden-mistral-key",
}
PASSTHROUGH_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN": "member-subscription-token",
    "HTTPS_PROXY": "http://proxy.example",
    "SSL_CERT_FILE": "/certs/ca.pem",
}
PARENT_CLIENT_ENV = dict(PASSTHROUGH_ENV)
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
    binary = tmp_path / "claude-stub"
    config = binary.with_suffix(".json")
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
    binary.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys, time
config = json.loads(pathlib.Path(__file__).with_suffix(".json").read_text())
state = pathlib.Path(config["state"])
index = len(list(state.glob("call-*.json")))
(state / f"call-{index}.json").write_text(json.dumps({
    "argv": sys.argv,
    "client_env": {
        name: os.environ.get(name)
        for name in (
            "CLAUDE_CONFIG_DIR", "CODEX_HOME", "VIBE_HOME", "CODEX_API_KEY",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
            "MISTRAL_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY",
            "SSL_CERT_FILE",
        )
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
    **changes,
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
        "discovery_dir": {
            "scope": "launch-project",
            "configuration": "run-owned --settings and --mcp-config",
        },
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
            "transport": "stdio",
            "command": os.environ.get("PYTHON", "/usr/bin/python3"),
            "args": ISOLATED_MEMORY_ARGS,
            "env_vars": ["MAGNOLIA_PROJECT_DIR"],
        },
        "invocation_id": invocation,
        "launch_argv": (str(binary),),
        "role_capabilities": {"claude-code": {role: evidence}},
        "environment": {
            "PATH": os.environ.get("PATH", ""),
            **PARENT_CLIENT_ENV,
        },
        "role_cap": role_cap,
        "governor_cap": governor_cap,
        "governance": governance,
        "timeout_seconds": 3,
    }
    values.update(changes)
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


def test_claude_rechecks_project_settings_after_admission(tmp_path):
    stream = _jsonl(tmp_path / "stream.jsonl", [{
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "late-settings", "result": "ok",
    }])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="helper")
    executor = executor_for(context.route, "helper", context)
    settings = context.project_dir / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ExecutorRefused, match="settings.json"):
        executor.run("prompt", context.project_dir, tmp_path / "attempt", retries=0)

    assert _calls(state) == []


def _synthetic_quota_detector(event):
    if event.get("synthetic") is not True or event.get("subtype") != "error":
        return None
    reset = event.get("reset_at")
    reset_at = datetime.fromisoformat(reset) if isinstance(reset, str) else None
    return RunOutcome(
        "quota",
        LimitInfo(
            "synthetic Claude session limit",
            reset_at,
            "synthetic Claude fixture; not live-certified",
        ),
    )


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
    assert result.session_identity.session_id == result.session_id
    assert result.resume_artifact.session_id == result.session_id
    assert result.resume_artifact.client == "claude-code"
    (call,) = _calls(state)
    argv = call["argv"]
    assert argv[0] == str(binary)
    assert argv[1:3] == ["-p", "work"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--max-turns") + 1] == "8"
    assert argv[argv.index("--settings") + 1].endswith(
        "/control/executors/claude-one/claude-settings.json"
    )
    assert argv[argv.index("--mcp-config") + 1].endswith(
        "/control/executors/claude-one/claude-mcp.json"
    )
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
    settings = json.loads(
        Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8")
    )
    assert {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "SessionEnd",
    } <= settings["hooks"].keys()
    pre_tool = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert pre_tool.startswith("/bin/sh -c ")
    assert "compchem_memory/hook/runner.py" in pre_tool
    assert " -I " in pre_tool
    assert "permissionDecision" in pre_tool
    mcp = json.loads(
        Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8")
    )
    assert mcp == {
        "mcpServers": {
            "compchem-memory": {
                "command": context.python_executable,
                "args": WORKER_MEMORY_ARGS,
                "env": {"MAGNOLIA_PROJECT_DIR": "${MAGNOLIA_PROJECT_DIR}"},
            }
        }
    }
    assert call["client_env"] == PASSTHROUGH_ENV
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
    assert mcp == {"mcpServers": {}}


def test_claude_reviewer_uses_strict_empty_native_mcp_config(tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, _ = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="reviewer")
    executor = executor_for(context.route, "reviewer", context)

    argv = executor._argv("review", None, 8)
    mcp_path = Path(argv[argv.index("--mcp-config") + 1])

    assert "--strict-mcp-config" in argv
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == {"mcpServers": {}}


@pytest.mark.parametrize("damage", ("missing", "invalid"))
def test_claude_refuses_missing_or_invalid_native_artifact(tmp_path, damage):
    stream = _jsonl(tmp_path / "success.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)
    executor = executor_for(context.route, "evaluator", context)
    settings = executor.prepared.invocation_dir / "claude-settings.json"
    settings.chmod(0o644)
    if damage == "missing":
        settings.unlink()
    else:
        settings.write_text("[]\n", encoding="utf-8")
        settings.chmod(0o444)

    with pytest.raises(ExecutorRefused, match="Claude-native launch artifact"):
        executor.run("evaluate", context.project_dir, tmp_path / "attempt")

    assert _calls(state) == []


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
    assert parsed.usage.uncached_input_tokens == 16
    assert parsed.usage.output_tokens == 1934
    assert parsed.usage.reasoning_tokens == 719
    assert parsed.usage.cached_tokens == 164374
    assert parsed.usage.cache_write_tokens == 9912
    assert parsed.usage.turns == 8
    assert parsed.model_usage["claude-haiku-5-5"]["cacheReadInputTokens"] == 164374
    assert parsed.terminal_reason == "completed"
    assert parsed.is_error is False
    assert parsed.usage.cost == pytest.approx(0.00459474)

    resumed = parse_claude_stream(
        "\n".join(
            json.dumps(_live_event("c4", name))
            for name in ("11-stream-init.json", "15-stream-result.json")
        ),
        resumed=True,
    )

    assert resumed.usage.input_tokens == 12
    assert resumed.usage.uncached_input_tokens == 12
    assert resumed.usage.output_tokens == 979
    assert resumed.usage.cached_tokens == 149350
    assert resumed.usage.cache_write_tokens == 2913
    assert resumed.usage.cost == pytest.approx(0.0025668)


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
    assert rows[0]["session_identity"]["session_id"] == "retry-session"
    assert rows[1]["session_identity"]["session_id"] == "retry-session"
    binding = json.loads(
        (tmp_path / "run/control/executors/claude-one/session-bindings.json").read_text()
    )
    assert binding["sessions"]["retry-session"]["role"] == "evaluator"


def test_claude_explicit_same_identity_resume_keeps_model_and_restrictions(tmp_path):
    first_stream = _jsonl(
        tmp_path / "first.jsonl",
        [_live_event("c1", "17-stream-init.json"), _live_event("c1", "24-stream-result.json")],
    )
    resumed_stream = _jsonl(
        tmp_path / "resumed.jsonl",
        [_live_event("c4", "11-stream-init.json"), _live_event("c4", "15-stream-result.json")],
    )
    binary, config, state = _stub(tmp_path, [first_stream, resumed_stream])
    first_context = _context(tmp_path, binary, config)
    first_executor = executor_for(first_context.route, "worker", first_context)
    first = first_executor.run("first", first_context.project_dir, tmp_path / "attempt-1")
    resumed_context = _context(
        tmp_path,
        binary,
        config,
        resume_from=first_executor.prepared.invocation_dir,
    )
    resumed_executor = executor_for(resumed_context.route, "worker", resumed_context)

    resumed = resumed_executor.run(
        "continue",
        resumed_context.project_dir,
        tmp_path / "attempt-2",
        resume_session=first.session_id,
    )

    assert resumed.outcome.kind == "success"
    first_argv, resumed_argv = (call["argv"] for call in _calls(state))
    assert resumed_argv[resumed_argv.index("--resume") + 1] == "ses-fixture-1"
    assert resumed_argv[resumed_argv.index("--model") + 1] == "haiku"
    for flag in ("--settings", "--mcp-config", "--strict-mcp-config", "--allowedTools"):
        assert flag in first_argv and flag in resumed_argv


@pytest.mark.parametrize("change", ("role", "policy"))
def test_claude_resume_refuses_other_bound_identity(tmp_path, change):
    stream = _jsonl(
        tmp_path / "first.jsonl",
        [_live_event("c1", "17-stream-init.json"), _live_event("c1", "24-stream-result.json")],
    )
    binary, config, _ = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config)
    executor = executor_for(context.route, "worker", context)
    first = executor.run("first", context.project_dir, tmp_path / "attempt-1")
    prepared = executor.prepared
    role = "reviewer" if change == "role" else "worker"
    if change == "policy":
        prepared = replace(prepared, policy_digest="different-policy")
    resumed = ClaudeCodeExecutor(
        prepared,
        executor.model,
        role,
        executor.max_turns,
        resume_permitted=True,
    )

    with pytest.raises(ExecutorRefused, match="bound identity"):
        resumed.run(
            "continue",
            context.project_dir,
            tmp_path / "attempt-2",
            resume_session=first.session_id,
        )


def test_claude_resume_refuses_unknown_session(tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config)
    executor = executor_for(context.route, "worker", context)
    resumed = replace(executor, resume_permitted=True)

    with pytest.raises(ExecutorRefused, match="unknown Claude session"):
        resumed.run(
            "continue",
            context.project_dir,
            tmp_path / "attempt",
            resume_session="not-recorded",
        )

    assert _calls(state) == []


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
    binary, config, state = _stub(
        tmp_path, [stream], returncodes=[returncode], linger_after_first=True
    )
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)

    executor = executor_for(context.route, "evaluator", context)
    executor.quota_detector = _synthetic_quota_detector
    result = executor.run(
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

    executor = executor_for(context.route, "evaluator", context)
    executor.quota_detector = _synthetic_quota_detector
    result = executor.run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "quota"
    assert result.outcome.limit.reset_at.isoformat() == reset
    assert result.attempts == 1
    assert not (state / "was-not-terminated").exists()


def test_claude_observed_quota_precedes_success_empty_retry(tmp_path):
    empty = _jsonl(
        tmp_path / "empty.jsonl",
        [
            {
                "type": "result",
                "subtype": "success",
                "session_id": "quota-session",
                "result": "",
                "is_error": False,
            }
        ],
    )
    binary, config, state = _stub(tmp_path, [empty])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)
    observed = RunOutcome(
        "quota", LimitInfo("observed quota", None, "injected test detector")
    )
    def observe_after_output(stdout, _stderr):
        return observed if stdout.read_text(encoding="utf-8") else None

    control = ProcessControl(observe=observe_after_output)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt", control=control
    )

    assert result.outcome.kind == "quota"
    assert result.attempts == 1
    assert len(_calls(state)) == 1
    assert result.session_identity.session_id == "quota-session"
    assert result.resume_artifact.session_id == "quota-session"


def test_claude_daily_limit_returns_without_client_or_attempt(tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [])
    binary, config, state = _stub(tmp_path, [stream])
    now = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
    reset = datetime(2026, 11, 17, tzinfo=timezone.utc)
    route = ResolvedRoute(
        "claude-code", "haiku", "claude-haiku", "claude-code", "evaluator", "test"
    )
    governor = Governor(
        Policy(max_runs_per_day=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=lambda: now,
    )
    prior = governor.admit(
        AdmissionRequest(route, "prior", None, "project-digest", "evaluator")
    )
    prior.mark_started(pid=1, pgid=1, process_start_token="prior")
    prior.release(outcome=RunOutcome("success"))
    governance = InvocationGovernance(
        governor, route, "parent-run", "project-digest", role_cap=1
    )
    context = _context(
        tmp_path, binary, config, role="evaluator", role_cap=1, governance=governance
    )

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "quota"
    assert result.outcome.limit.reason == "daily_limit"
    assert result.outcome.limit.reset_at == reset
    assert result.outcome.limit.hold_id
    assert result.outcome.limit.hold_id == governor.quota_wait("claude-code").hold_id
    assert result.attempts == 0
    assert _calls(state) == []


def test_claude_resume_artifact_round_trip_and_mismatch_refusal(tmp_path):
    stream = _jsonl(
        tmp_path / "first.jsonl",
        [{"type": "result", "subtype": "success", "session_id": "bound", "result": "ok", "is_error": False}],
    )
    resumed = _jsonl(
        tmp_path / "resumed.jsonl",
        [{"type": "result", "subtype": "success", "session_id": "bound", "result": "again", "is_error": False}],
    )
    binary, config, state = _stub(tmp_path, [stream, resumed])
    first_context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)
    first_executor = executor_for(first_context.route, "evaluator", first_context)
    first = first_executor.run("first", first_context.project_dir, tmp_path / "attempt-1")
    resume_context = replace(
        first_context,
        resume_from=first_executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    second = executor_for(resume_context.route, "evaluator", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    assert second.session_id == "bound"
    resume_argv = _calls(state)[1]["argv"]
    assert resume_argv[resume_argv.index("--resume") + 1] == "bound"
    wrong_client = replace(first.resume_artifact, client="codex")
    with pytest.raises(ExecutorRefused, match="resume artifact"):
        executor_for(
            resume_context.route,
            "evaluator",
            replace(resume_context, resume_artifact=wrong_client),
        )
    wrong_policy = replace(
        first.resume_artifact, configuration_identity_digest="different-policy"
    )
    with pytest.raises(ExecutorRefused, match="resume artifact"):
        executor_for(
            resume_context.route,
            "evaluator",
            replace(resume_context, resume_artifact=wrong_policy),
        )


def test_claude_refuses_forged_resume_session_without_starting_client(tmp_path):
    stream = _jsonl(
        tmp_path / "first.jsonl",
        [{"type": "result", "subtype": "success", "session_id": "bound", "result": "ok", "is_error": False}],
    )
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)
    executor = executor_for(context.route, "evaluator", context)
    result = executor.run("first", context.project_dir, tmp_path / "attempt-1")
    binding_path = executor.prepared.invocation_dir / "session-bindings.json"
    original_binding = binding_path.read_bytes()
    forged = replace(result.resume_artifact, session_id="forged")

    with pytest.raises(ExecutorRefused, match="unknown Claude session"):
        executor_for(
            context.route,
            "evaluator",
            replace(
                context,
                resume_from=executor.prepared.invocation_dir,
                resume_artifact=forged,
            ),
        )

    assert binding_path.read_bytes() == original_binding
    assert len(_calls(state)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("configuration_identity_digest", "different-digest"),
        ("role", "helper"),
        ("project_identity", "different-project"),
        ("client", "codex"),
    ),
)
def test_claude_refuses_resume_session_rebound_in_store(tmp_path, field, value):
    stream = _jsonl(
        tmp_path / "first.jsonl",
        [{"type": "result", "subtype": "success", "session_id": "bound", "result": "ok", "is_error": False}],
    )
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)
    executor = executor_for(context.route, "evaluator", context)
    result = executor.run("first", context.project_dir, tmp_path / "attempt-1")
    binding_path = executor.prepared.invocation_dir / "session-bindings.json"
    payload = json.loads(binding_path.read_text())
    payload["sessions"]["bound"][field] = value
    binding_path.write_text(json.dumps(payload))

    with pytest.raises(ExecutorRefused, match="different bound identity"):
        executor_for(
            context.route,
            "evaluator",
            replace(
                context,
                resume_from=executor.prepared.invocation_dir,
                resume_artifact=result.resume_artifact,
            ),
        )

    assert len(_calls(state)) == 1


def test_claude_argv_failure_releases_reservation(tmp_path, monkeypatch):
    stream = _jsonl(tmp_path / "unused.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, _ = _stub(tmp_path, [stream])
    governor = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
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
    executor = executor_for(context.route, "evaluator", context)

    def fail_argv():
        raise WorkerError("argv failed")

    monkeypatch.setattr(executor, "_allowed_tools", fail_argv)
    with pytest.raises(WorkerError, match="argv failed"):
        executor.run("evaluate", context.project_dir, tmp_path / "attempt")

    assert governor._load_state()["reservations"] == {}


def test_claude_native_tools_flag_is_exact(monkeypatch, tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, _ = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="helper")
    executor = executor_for(context.route, "helper", context)
    monkeypatch.setattr(executor, "_allowed_tools", lambda: (["Read", "Grep"], []))

    argv = executor._argv("help", None, 8)

    assert argv[argv.index("--tools") : argv.index("--tools") + 2] == [
        "--tools",
        "Read,Grep",
    ]


def test_claude_refuses_extra_or_prefixed_forbidden_argv(tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, _ = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)
    executor = executor_for(context.route, "evaluator", context)

    with pytest.raises(WorkerError, match="exactly one executable"):
        replace(executor, prepared=replace(executor.prepared, argv=(str(binary), "--verbose")))
    with pytest.raises(WorkerError, match="permission-mode"):
        replace(
            executor,
            prepared=replace(
                executor.prepared,
                argv=("--permission-mode=bypassPermissions",),
            ),
        )


def test_claude_hold_before_exec_is_not_an_attempt(tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, state = _stub(tmp_path, [stream])
    governor = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
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
    checks = 0

    def stop_after_admission():
        nonlocal checks
        checks += 1
        return None if checks == 1 else "cancel before exec"

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate",
        context.project_dir,
        tmp_path / "attempt",
        control=ProcessControl(should_stop=stop_after_admission),
    )

    assert result.outcome.kind == "cancelled"
    assert result.attempts == 0
    assert _calls(state) == []


def test_claude_preserves_outer_start_barrier_without_inner_governance(tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [_live_event("c7", "07-stream-result.json")])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary, config, role="evaluator", role_cap=1)

    def reject_start(_pid, _pgid):
        raise RuntimeError("outer admission could not record process start")

    with pytest.raises(WorkerError, match="outer admission could not record process start"):
        executor_for(context.route, "evaluator", context).run(
            "evaluate",
            context.project_dir,
            tmp_path / "attempt",
            control=ProcessControl(on_start=reject_start, hold_before_exec=True),
        )

    assert _calls(state) == []


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
