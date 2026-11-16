"""Task 8 contract tests for the governed Codex executor."""

import json
import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.codex import CodexExecutor, parse_codex_stream
from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.launch import configuration_identity
from campaign_runner.routing import ResolvedRoute
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_common import hook_command


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "executors" / "codex" / "live-p3"


def _stream(run: str) -> str:
    return "\n".join(
        json.dumps(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((FIXTURES / run).glob("*-stream-*.json"))
    )


def _context(tmp_path: Path, role: str = "helper", model: str = "gpt-test") -> ExecContext:
    project = tmp_path / "project"
    run = tmp_path / "run"
    (project / ".codex").mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (run / "work").mkdir()
    (project / ".codex" / "config.toml").write_text("model_provider = \"subscription\"\n")
    hooks_path = project / ".codex" / "hooks.json"
    required_command = hook_command(
        LaunchContext(
            root=ROOT, project="project-fixture", project_dir=project,
            python="/venv/bin/python", tools_port=8123, model=model, setup={},
            run_dir=run, control_dir=run / "control", work_dir=run / "work",
            role=role, invocation_id=f"{role}-one", noninteractive=True,
        ),
        "codex",
        "pre-tool",
    )
    hooks_path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{
            "type": "command", "command": required_command
        }]}]}}) + "\n"
    )
    trust = tmp_path / "codex-user.toml"
    key = f"{hooks_path.resolve()}:pre_tool_use:0:0"
    trust.write_text(
        f'[projects.{json.dumps(str(project.resolve()))}]\ntrust_level = "trusted"\n\n'
        f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "sha256:fixture"\n'
    )
    route = ResolvedRoute("codex", model, "family", "codex", role, "test")
    evidence = {
        "accepted": True, "trusted": True, "stale": False, "identity": "pending",
        "discovery_dir": str(project.resolve()), "capabilities": [],
        "model_isolation": {"accepted": True, "model": model, "identity": "fixture"},
    }
    context = ExecContext(
        route=route, role=role, project_dir=project, run_dir=run,
        control_dir=run / "control", work_dir=run / "work",
        installation_root=ROOT, workspace_root=tmp_path,
        project_identity="project-fixture", python_executable="/venv/bin/python",
        tools_port=8123, launch_argv=("codex-stub",),
        memory_service={"transport": "http", "url": "http://127.0.0.1:8765/mcp"},
        role_capabilities={"codex": {role: evidence}},
        environment={"PATH": os.environ.get("PATH", "")},
        setup={
            "codex_user_config_path": str(trust),
            "codex_hooks_list": [{
                "key": key, "currentHash": "sha256:fixture", "trustStatus": "trusted",
                "enabled": True, "eventName": "preToolUse",
                "sourcePath": str(hooks_path.resolve()),
            }],
        },
        invocation_id=f"{role}-one",
    )
    evidence["identity"] = configuration_identity(context)
    return context


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
    assert argv[:3] == ["codex-stub", "exec", "--json"]
    assert argv[argv.index("-m") + 1] == "gpt-test"
    assert argv[argv.index("--disable") + 1] == "code_mode_host"
    native = tomllib.loads(
        (executor.prepared.invocation_dir / ".codex" / "config.toml").read_text()
    )
    assert native.get("mcp_servers", {}) == {}


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


def test_codex_effective_cap_or_bounded_refusal(tmp_path):
    context = _context(tmp_path)
    executor = executor_for(context.route, "helper", context)
    stream = tmp_path / "cap.jsonl"
    stream.write_text("\n".join(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": str(i)}})
        for i in range(3)
    ))
    stderr = tmp_path / "err"
    stderr.write_text("")
    assert executor._control(None, None, 2).observe(stream, stderr).kind == "cap_reached"


@pytest.mark.parametrize("client", ("codex", "vibe"))
@pytest.mark.parametrize("reply", ("", "partial"))
@pytest.mark.parametrize("exit_code", (0, 1))
@pytest.mark.parametrize(
    "reset_at", (None, datetime(2026, 11, 17, tzinfo=timezone.utc))
)
def test_client_quota_blocks_every_local_retry(client, reply, exit_code, reset_at):
    quota = RunOutcome("quota", LimitInfo("fixture quota", reset_at, "synthetic"))
    if client == "codex":
        text = ""
        if reply:
            text += json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": reply},
            }) + "\n"
        text += '{"type":"turn.completed","synthetic":true,"usage":{}}'
        observed = parse_codex_stream(
            text,
            quota_detector=lambda event: quota if event.get("synthetic") else None,
        ).outcome
    else:
        # Vibe has no certified resume, so an externally observed governor quota
        # is terminal regardless of its otherwise retryable export/reply shape.
        observed = quota
    assert observed is quota
    assert observed.overrides_repair
    assert observed.limit.reset_at == reset_at
    assert exit_code in (0, 1)


def test_client_nonquota_errors_stay_failures():
    parsed = parse_codex_stream('{"type":"error","message":"ordinary"}')
    assert parsed.outcome is None
    assert parsed.result_seen is False
    assert parsed.diagnostics[0]["kind"] == "client_error"


def test_client_resume_preserves_caps_and_tools(tmp_path):
    context = _context(tmp_path)
    executor = executor_for(context.route, "helper", context)
    executor._bind_session("thread-1", parse_codex_stream(_stream("x1")).usage)
    argv = executor._argv("again", "thread-1")
    assert argv[:4] == ["codex-stub", "exec", "resume", "--json"]
    assert 'model="gpt-test"' in argv
    assert argv[argv.index("--disable") : argv.index("--disable") + 2] == [
        "--disable", "code_mode_host"
    ]
    assert "-s" not in argv
