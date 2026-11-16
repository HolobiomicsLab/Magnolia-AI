"""Task 8 contract tests for the governed Vibe executor."""

import json
import os
import tomllib
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.vibe import VibeExecutor, parse_vibe_export, require_vibe_trust
from campaign_runner.launch import configuration_identity
from campaign_runner.routing import ResolvedRoute
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_common import hook_command


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "executors" / "vibe" / "live-p3"


def _export(run: str) -> str:
    return next((FIXTURES / run).glob("*-stream-export.json")).read_text(encoding="utf-8")


def _context(tmp_path: Path) -> ExecContext:
    project = tmp_path / "project"
    run = tmp_path / "run"
    (project / ".vibe").mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (run / "work").mkdir()
    (project / ".vibe" / "config.toml").write_text(
        'active_model = "selected-model"\ndisabled_tools = ["file_system.bash"]\n'
    )
    required_command = hook_command(
        LaunchContext(
            root=ROOT, project="project-fixture", project_dir=project,
            python="/venv/bin/python", tools_port=8123, model="selected-model",
            setup={}, run_dir=run, control_dir=run / "control",
            work_dir=run / "work", role="reviewer", invocation_id="reviewer-one",
            noninteractive=True,
        ),
        "vibe",
        "pre-tool",
    )
    (project / ".vibe" / "hooks.toml").write_text(
        '[[hooks]]\nname = "magnolia-pre-tool"\ntype = "pre_tool"\n'
        f'command = {json.dumps(required_command)}\nstrict = true\n'
    )
    trust = tmp_path / "trusted_folders.toml"
    trust.write_text(f'trusted = [{json.dumps(str(project.resolve()))}]\nuntrusted = []\n')
    route = ResolvedRoute("vibe", "selected-model", "family", "vibe", "reviewer", "test")
    evidence = {
        "accepted": True, "trusted": True, "stale": False, "identity": "pending",
        "discovery_dir": str(project.resolve()),
        "capabilities": ["native_read", "native_glob", "native_grep"],
        "model_isolation": {
            "accepted": True, "model": "selected-model", "identity": "fixture"
        },
    }
    context = ExecContext(
        route=route, role="reviewer", project_dir=project, run_dir=run,
        control_dir=run / "control", work_dir=run / "work",
        installation_root=ROOT, workspace_root=tmp_path,
        project_identity="project-fixture", python_executable="/venv/bin/python",
        tools_port=8123, launch_argv=("vibe-stub",),
        role_capabilities={"vibe": {"reviewer": evidence}},
        environment={"PATH": os.environ.get("PATH", "")},
        setup={"vibe_trust_path": str(trust)}, invocation_id="reviewer-one",
    )
    evidence["identity"] = configuration_identity(context)
    return context


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
    assert argv[argv.index("--disabled-tools") + 1] == "file_system.bash"
    assert executor.prepared.env["VIBE_ENABLE_TELEMETRY"] == "false"
    assert "VIBE_DISABLE_TELEMETRY" not in executor.prepared.env
    hooks = tomllib.loads(
        (executor.prepared.invocation_dir / ".vibe" / "hooks.toml").read_text()
    )["hooks"]
    assert any(hook.get("type") == "pre_tool" and hook.get("strict") is True for hook in hooks)


def test_vibe_worker_edits_require_D4_and_gate():
    route = ResolvedRoute("vibe", "model", "family", "vibe", "worker", "test")
    context = ExecContext(
        route=route,
        role="worker",
        role_capabilities={"vibe": {"worker": {"accepted": True}}},
    )
    with pytest.raises(ExecutorRefused, match="D4"):
        executor_for(route, "worker", context)


def test_vibe_model_isolated_between_runs():
    accepted = parse_vibe_export(_export("v1"))
    fallback = parse_vibe_export(_export("v2"))
    assert accepted.reported_model == "zai-glm-5-3-flash"
    assert fallback.reported_model == "Mistral Medium 3.5"
    assert fallback.reported_model != accepted.reported_model


def test_vibe_export_usage_cache_and_redaction():
    parsed = parse_vibe_export(_export("v2"))
    assert parsed.usage.input_tokens == 135962
    assert parsed.usage.cached_tokens == 115456
    assert parsed.usage.cost == pytest.approx(0.0540174)
    assert parsed.outcome.kind == "price_limit"
    assert parsed.turns == 6


def test_vibe_parser_survives_garbage():
    parsed = parse_vibe_export("not-json")
    assert parsed.outcome.kind == "failure"
    assert parsed.usage.input_tokens is None
    assert parsed.diagnostics[0]["kind"] == "invalid_json"
