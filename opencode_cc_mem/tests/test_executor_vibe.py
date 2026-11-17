"""Task 8 contract tests for the governed Vibe executor."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.executors.vibe import VibeExecutor, parse_vibe_export, require_vibe_trust
from campaign_runner.governor import AdmissionRequest, Governor, Policy
from campaign_runner.launch import configuration_identity
from campaign_runner.process import ProcessControl
from campaign_runner.routing import ResolvedRoute
from campaign_runner.workers import InvocationGovernance
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_vibe import native_artifacts


ROOT = Path(__file__).parents[1]
STRIPPED_ENV = {
    "CLAUDE_CONFIG_DIR": "forbidden-claude-config",
    "CODEX_HOME": "forbidden-codex-home",
    "VIBE_HOME": "forbidden-vibe-home",
    "CODEX_API_KEY": "forbidden-codex-key",
    "ANTHROPIC_API_KEY": "forbidden-anthropic-key",
    "ANTHROPIC_AUTH_TOKEN": "forbidden-anthropic-token",
    "OPENAI_API_KEY": "forbidden-openai-key",
    "MISTRAL_API_KEY": "forbidden-mistral-key",
}
PASSTHROUGH_ENV = {
    "VENDOR_SPECIFIC_API_KEY": "unlisted-provider-key",
    "MAGNOLIA_OPENAI_API_KEY": "magnolia-openai-key",
    "MAGNOLIA_MISTRAL_API_KEY": "magnolia-mistral-key",
    "OPENROUTER_API_KEY": "forbidden-openrouter-key",
    "DEEPSEEK_API_KEY": "forbidden-deepseek-key",
    "KIMI_PLAN_MAGNOLIA_API_KEY": "forbidden-kimi-plan-key",
    "KIMI_API_KEY": "forbidden-kimi-key",
    "ZHIPU_API_KEY": "forbidden-zhipu-key",
    "ZAI_API_KEY": "forbidden-zai-key",
    "MAGNOLIA_LLM_API_KEY": "forbidden-memory-key",
    "MAGNOLIA_CRITIC_API_KEY": "forbidden-critic-key",
}
PARENT_CLIENT_ENV = {**STRIPPED_ENV, **PASSTHROUGH_ENV}


def _real_export(
    *,
    model: str = "selected-model",
    warnings: list[dict] | None = None,
    outcome: str = "finished",
    exit_code: int = 0,
) -> dict:
    """Return the installed Vibe 3.1.2 ``RunExport`` shape."""
    payload = {
        "outcome": outcome,
        "stop_reason": None,
        "error": None,
        "session_id": "vibe-session",
        "usage": {
            "input_tokens": 135962,
            "output_tokens": 792,
            "cached_input_tokens": 115456,
            "total_tokens": 136754,
        },
        "cost_usd": 0.0540174,
        "steps": 6,
        "config": {
            "active_model": model,
            "enable_telemetry": False,
            "disabled_tools": ["file_system.bash"],
            "models": {"selected-model": {"provider": "fixture"}},
        },
        "schema_version": 1,
        "vibe_version": "3.1.2",
        "journal_dir": "session",
        "started_at": "2026-11-16T07:00:00Z",
        "ended_at": "2026-11-16T07:00:05Z",
        "limits": {
            "max_turns": 8,
            "max_price": 0.05,
            "max_tokens": None,
            "time_limit_s": None,
        },
        "exit_code": exit_code,
    }
    if warnings is not None:
        payload["warnings"] = warnings
    return payload


def _fallback_export() -> dict:
    return _real_export(
        model="",
        warnings=[{
            "message": (
                "Model 'selected-model' is not available — running "
                "'Mistral Medium 3.5'. Pick another model to stop this notice."
            ),
            "source": None,
        }],
    )


def _stub(
    tmp_path: Path,
    exports: list[Path | None],
    *,
    stdout_lines: list[list[dict]] | None = None,
    linger_after_first: bool = False,
) -> tuple[Path, Path, Path]:
    state = tmp_path / "stub-state"
    state.mkdir()
    config = tmp_path / "vibe-stub.json"
    config.write_text(json.dumps({
        "exports": [str(path) if path is not None else None for path in exports],
        "stdout_lines": stdout_lines or [[] for _ in exports],
        "state": str(state),
        "linger_after_first": linger_after_first,
    }))
    binary = tmp_path / "vibe-stub"
    binary.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
config = json.loads(pathlib.Path(os.environ["VIBE_STUB_CONFIG"]).read_text())
state = pathlib.Path(config["state"])
index = len(list(state.glob("call-*.json")))
argv = sys.argv
output_dir = pathlib.Path(argv[argv.index("--output-dir") + 1])
output_dir.mkdir(parents=True, exist_ok=True)
(state / f"call-{index}.json").write_text(json.dumps({
    "argv": argv,
    "cwd": os.getcwd(),
    "client_env": {
        name: os.environ.get(name)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "VIBE_HOME", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "MISTRAL_API_KEY", "MAGNOLIA_OPENAI_API_KEY", "MAGNOLIA_MISTRAL_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY", "ZHIPU_API_KEY", "ZAI_API_KEY", "MAGNOLIA_LLM_API_KEY", "MAGNOLIA_CRITIC_API_KEY",)
        if name in os.environ
    },
    "telemetry": os.environ.get("VIBE_ENABLE_TELEMETRY"),
}))
source = config["exports"][index]
if source:
    shutil.copy(source, output_dir / "export.json")
for event in config["stdout_lines"][index]:
    print(json.dumps(event), flush=True)
if index == 0 and config["linger_after_first"]:
    time.sleep(5)
    (state / "was-not-terminated").write_text("late")
"""
    )
    binary.chmod(0o755)
    return binary, config, state


def _context(
    tmp_path: Path,
    *,
    binary: Path | None = None,
    stub_config: Path | None = None,
    governance: InvocationGovernance | None = None,
) -> ExecContext:
    project = tmp_path / "project"
    run = tmp_path / "run"
    (project / ".vibe").mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (run / "work").mkdir()
    launch_context = LaunchContext(
        root=ROOT, project="project-fixture", project_dir=project,
        python="/venv/bin/python", tools_port=8123, model="selected-model",
        setup={}, run_dir=run, control_dir=run / "control",
        work_dir=run / "work", role="reviewer", invocation_id="reviewer-one",
        noninteractive=True,
    )
    for relative, body in native_artifacts(launch_context, []).items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    trust = tmp_path / "trusted_folders.toml"
    trust.write_text(f'trusted = [{json.dumps(str(project.resolve()))}]\nuntrusted = []\n')
    route = ResolvedRoute(
        "vibe", "selected-model", "family", "vibe", "reviewer", "test"
    )
    evidence = {
        "accepted": True, "trusted": True, "stale": False, "identity": "pending",
        "discovery_dir": {
            "scope": "launch-project",
            "configuration": "run-owned project config",
        },
        "capabilities": ["native_read", "native_glob", "native_grep"],
        "model_isolation": {
            "accepted": True, "model": "selected-model", "identity": "fixture"
        },
    }
    environment = {"PATH": os.environ.get("PATH", ""), **PARENT_CLIENT_ENV}
    if stub_config is not None:
        environment["VIBE_STUB_CONFIG"] = str(stub_config)
    context = ExecContext(
        route=route, role="reviewer", project_dir=project, run_dir=run,
        control_dir=run / "control", work_dir=run / "work",
        installation_root=ROOT, workspace_root=tmp_path,
        project_identity="project-fixture", python_executable="/venv/bin/python",
        tools_port=8123, launch_argv=(str(binary or "vibe-stub"),),
        role_capabilities={"vibe": {"reviewer": evidence}},
        environment=environment,
        setup={"vibe_trust_path": str(trust)},
        governance=governance,
        timeout_seconds=3,
        invocation_id="reviewer-one",
    )
    evidence["identity"] = configuration_identity(context)
    return context


def _calls(state: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(state.glob("call-*.json"))]


def _governance(tmp_path: Path) -> InvocationGovernance:
    governor = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
    )
    route = ResolvedRoute(
        "vibe", "selected-model", "family", "vibe", "reviewer", "test"
    )
    return InvocationGovernance(
        governor, route, "parent-run", "project-fixture", role_cap=8
    )


def test_vibe_canonical_ancestor_trust_and_project_source(tmp_path):
    trusted = tmp_path / "trusted"
    project = trusted / "nested" / "project"
    project.mkdir(parents=True)
    trust = tmp_path / "trusted_folders.toml"
    trust.write_text(
        f'trusted = [{json.dumps(str(trusted.resolve()))}]\n'
        f'untrusted = [{json.dumps(str((trusted / "nested").resolve()))}]\n'
    )
    with pytest.raises(ExecutorRefused, match="nearest untrusted"):
        require_vibe_trust(project, {"vibe_trust_path": str(trust)})
    trust.write_text(f'trusted = [{json.dumps(str(project.resolve()))}]\nuntrusted = []\n')
    assert require_vibe_trust(project, {"vibe_trust_path": str(trust)}) == project.resolve()


def test_vibe_no_trust_bypass_and_required_policy_env(tmp_path):
    context = _context(tmp_path)
    executor = executor_for(context.route, "reviewer", context)
    assert isinstance(executor, VibeExecutor)
    argv = executor._argv("prompt", tmp_path / "out", 8)
    assert "--trust" not in argv
    assert "--auto-approve" in argv
    disabled = [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "--disabled-tools"
    ]
    assert disabled == [
        "file_system.bash", "file_system.edit", "file_system.write_file",
        "process.start", "process.write",
    ]
    assert executor.prepared.env["VIBE_ENABLE_TELEMETRY"] == "false"
    assert "VIBE_DISABLE_TELEMETRY" not in executor.prepared.env


def test_vibe_releases_managed_memory_after_client_exit(tmp_path, monkeypatch):
    context = _context(tmp_path)
    executor = executor_for(context.route, "reviewer", context)
    handle = object()
    object.__setattr__(executor.prepared, "memory_service_handle", handle)
    released = []
    sentinel = object()
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda value: released.append(value) or True,
    )
    monkeypatch.setattr(VibeExecutor, "_run", lambda self, *args, **kwargs: sentinel)

    result = executor.run("prompt", tmp_path, tmp_path / "attempt")

    assert result is sentinel
    assert released == [handle]
    assert executor.prepared.memory_service_handle is None


def test_vibe_refusal_after_prepare_releases_managed_memory(tmp_path, monkeypatch):
    context = _context(tmp_path)
    context = replace(
        context,
        setup={**context.setup, "max_price": 0},
    )
    handle = object()
    prepared = type("Prepared", (), {"memory_service_handle": handle})()
    released = []
    monkeypatch.setattr("campaign_runner.launch.prepare_launch", lambda value: prepared)
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda value: released.append(value) or True,
    )

    with pytest.raises(ExecutorRefused, match="max_price"):
        executor_for(context.route, "reviewer", context)

    assert released == [handle]


def test_vibe_worker_edits_require_D4_and_gate():
    route = ResolvedRoute("vibe", "model", "family", "vibe", "worker", "test")
    context = ExecContext(
        route=route,
        role="worker",
        role_capabilities={"vibe": {"worker": {"accepted": True}}},
    )
    with pytest.raises(ExecutorRefused, match="D4"):
        executor_for(route, "worker", context)


def test_vibe_real_export_selected_model_and_fallback_notice():
    accepted = parse_vibe_export(json.dumps(_real_export()))
    fallback = parse_vibe_export(json.dumps(_fallback_export()))
    assert accepted.reported_model == "selected-model"
    assert accepted.outcome.kind == "success"
    assert fallback.reported_model == "Mistral Medium 3.5"
    assert fallback.reported_model != accepted.reported_model


def test_vibe_export_usage_cache_and_redaction():
    parsed = parse_vibe_export(json.dumps(_real_export(outcome="price_limit", exit_code=3)))
    assert parsed.usage.input_tokens == 135962
    assert parsed.usage.uncached_input_tokens == 20506
    assert parsed.usage.cached_tokens == 115456
    assert parsed.usage.cost == pytest.approx(0.0540174)
    assert parsed.outcome.kind == "price_limit"
    assert parsed.turns == 6


def test_vibe_parser_survives_garbage():
    parsed = parse_vibe_export("not-json")
    assert parsed.outcome.kind == "failure"
    assert parsed.usage.input_tokens is None
    assert parsed.diagnostics[0]["kind"] == "invalid_json"


@pytest.mark.parametrize(
    ("payload", "expected"),
    ((_real_export(), "success"), (_fallback_export(), "model_mismatch"), (None, "failure")),
)
def test_vibe_run_real_export_model_outcomes_and_environment(tmp_path, payload, expected):
    export = tmp_path / "export.json"
    if payload is not None:
        export.write_text(json.dumps(payload))
    binary, config, state = _stub(
        tmp_path,
        [export if payload is not None else None],
        stdout_lines=[[{"type": "assistant", "content": "REVIEW-OK"}]],
    )
    governance = _governance(tmp_path)
    context = _context(
        tmp_path, binary=binary, stub_config=config, governance=governance
    )

    result = executor_for(context.route, "reviewer", context).run(
        "review", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == expected
    if payload is None:
        assert result.session_identity is None
        assert result.resume_artifact is None
    else:
        assert result.session_identity.session_id == "vibe-session"
        assert result.resume_artifact.session_id == "vibe-session"
    (call,) = _calls(state)
    assert call["client_env"] == {}
    assert call["telemetry"] == "false"
    rows = (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    assert len(rows) == 1
    if payload is not None:
        assert json.loads(rows[0])["session_identity"]["session_id"] == "vibe-session"
    diagnostics = [item["kind"] for item in result.errors]
    assert ("model_mismatch" in diagnostics) is (expected == "model_mismatch")


def test_vibe_quota_precedes_success_empty_without_retry(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps(_real_export()))
    binary, config, state = _stub(
        tmp_path,
        [export],
        stdout_lines=[[{"type": "status"}]],
        linger_after_first=True,
    )
    context = _context(tmp_path, binary=binary, stub_config=config, governance=_governance(tmp_path))
    quota = RunOutcome("quota", LimitInfo("fixture quota", None, "synthetic"))

    def observe_after_output(stdout: Path, _stderr: Path):
        return quota if stdout.read_text() else None

    result = executor_for(context.route, "reviewer", context).run(
        "review",
        context.project_dir,
        tmp_path / "attempt",
        retries=0,
        control=ProcessControl(observe=observe_after_output),
    )

    assert result.outcome.kind == "quota"
    assert result.final_text == ""
    assert result.attempts == 1
    assert len(_calls(state)) == 1
    assert not (state / "was-not-terminated").exists()
    rows = (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["outcome"]["kind"] == "quota"
    assert result.session_identity.session_id == "vibe-session"
    assert result.resume_artifact.session_id == "vibe-session"


def test_vibe_daily_limit_returns_without_client_or_attempt(tmp_path):
    export = tmp_path / "unused.json"
    export.write_text(json.dumps(_real_export()))
    binary, config, state = _stub(tmp_path, [export])
    now = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
    reset = datetime(2026, 11, 17, tzinfo=timezone.utc)
    route = ResolvedRoute(
        "vibe", "selected-model", "family", "vibe", "reviewer", "test"
    )
    governor = Governor(
        Policy(max_runs_per_day=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=lambda: now,
    )
    prior = governor.admit(
        AdmissionRequest(route, "prior", None, "project-fixture", "reviewer")
    )
    prior.mark_started(pid=1, pgid=1, process_start_token="prior")
    prior.release(outcome=RunOutcome("success"))
    governance = InvocationGovernance(
        governor, route, "parent-run", "project-fixture", role_cap=8
    )
    context = _context(
        tmp_path, binary=binary, stub_config=config, governance=governance
    )

    result = executor_for(context.route, "reviewer", context).run(
        "review", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "quota"
    assert result.outcome.limit.reason == "daily_limit"
    assert result.outcome.limit.reset_at == reset
    assert result.outcome.limit.hold_id
    assert result.outcome.limit.hold_id == governor.quota_wait("vibe").hold_id
    assert result.attempts == 0
    assert _calls(state) == []


def test_vibe_resume_artifact_round_trip_and_mismatch_refusal(tmp_path):
    first_export = tmp_path / "first.json"
    first_export.write_text(json.dumps(_real_export()))
    resumed_export = tmp_path / "resumed.json"
    resumed_export.write_text(json.dumps(_real_export()))
    binary, config, state = _stub(tmp_path, [first_export, resumed_export])
    first_context = _context(tmp_path, binary=binary, stub_config=config)
    first_executor = executor_for(first_context.route, "reviewer", first_context)
    first = first_executor.run("first", first_context.project_dir, tmp_path / "attempt-1")
    resume_context = replace(
        first_context,
        resume_from=first_executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    second = executor_for(resume_context.route, "reviewer", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    assert second.session_id == "vibe-session"
    resume_argv = _calls(state)[1]["argv"]
    assert resume_argv[resume_argv.index("--resume") + 1] == "vibe-session"
    wrong_project = replace(first.resume_artifact, project_identity="other-project")
    with pytest.raises(ExecutorRefused, match="resume artifact"):
        executor_for(
            resume_context.route,
            "reviewer",
            replace(resume_context, resume_artifact=wrong_project),
        )


def test_vibe_refuses_forged_resume_session_without_starting_client(tmp_path):
    export = tmp_path / "first.json"
    export.write_text(json.dumps(_real_export()))
    binary, config, state = _stub(tmp_path, [export])
    context = _context(tmp_path, binary=binary, stub_config=config)
    executor = executor_for(context.route, "reviewer", context)
    result = executor.run("first", context.project_dir, tmp_path / "attempt-1")
    binding_path = executor.prepared.invocation_dir / "vibe-session-bindings.json"
    original_binding = binding_path.read_bytes()
    forged = replace(result.resume_artifact, session_id="forged")

    with pytest.raises(ExecutorRefused, match="unknown Vibe session"):
        executor_for(
            context.route,
            "reviewer",
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
def test_vibe_refuses_resume_session_rebound_in_store(tmp_path, field, value):
    export = tmp_path / "first.json"
    export.write_text(json.dumps(_real_export()))
    binary, config, state = _stub(tmp_path, [export])
    context = _context(tmp_path, binary=binary, stub_config=config)
    executor = executor_for(context.route, "reviewer", context)
    result = executor.run("first", context.project_dir, tmp_path / "attempt-1")
    binding_path = executor.prepared.invocation_dir / "vibe-session-bindings.json"
    payload = json.loads(binding_path.read_text())
    payload["sessions"]["vibe-session"][field] = value
    binding_path.write_text(json.dumps(payload))

    with pytest.raises(ExecutorRefused, match="different bound identity"):
        executor_for(
            context.route,
            "reviewer",
            replace(
                context,
                resume_from=executor.prepared.invocation_dir,
                resume_artifact=result.resume_artifact,
            ),
        )

    assert len(_calls(state)) == 1


@pytest.mark.parametrize("damage", ("hook", "mcp", "tool", "model"))
def test_vibe_refuses_each_project_native_drift(tmp_path, damage):
    context = _context(tmp_path)
    if damage == "hook":
        path = context.project_dir / ".vibe" / "hooks.toml"
        path.write_text(path.read_text().replace("magnolia-pre-tool", "drifted-pre-tool"))
    else:
        path = context.project_dir / ".vibe" / "config.toml"
        replacements = {
            "mcp": '\n[[mcp_servers]]\nname = "drift"\ntransport = "stdio"\ncommand = "drift"\n',
            "tool": 'disabled_tools = ["file_system.bash", "process.start"]',
            "model": 'active_model = "drifted-model"',
        }
        body = path.read_text()
        if damage == "mcp":
            body += replacements[damage]
        else:
            key = "disabled_tools" if damage == "tool" else "active_model"
            body = "\n".join(
                replacements[damage] if line.startswith(key + " ") else line
                for line in body.splitlines()
            ) + "\n"
        path.write_text(body)
    context.role_capabilities["vibe"]["reviewer"]["identity"] = configuration_identity(context)

    with pytest.raises(ExecutorRefused, match="project.*Vibe|Vibe.*project|selected model"):
        executor_for(context.route, "reviewer", context)
