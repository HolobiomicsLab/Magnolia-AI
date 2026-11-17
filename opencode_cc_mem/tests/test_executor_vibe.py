"""Task 8 contract tests for the governed Vibe executor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.executors.vibe import VibeExecutor, parse_vibe_export, require_vibe_trust
from campaign_runner.governor import AdmissionRequest, Governor, Policy
from campaign_runner.launch import configuration_identity, prepare_launch
from campaign_runner.process import ProcessControl
from campaign_runner.routing import ResolvedRoute
from campaign_runner.workers import InvocationGovernance
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_vibe import (
    READ_ONLY_DISABLED_TOOLS,
    disabled_tools_for_role,
    native_artifacts,
)


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
    "OPENAI_BASE_URL": "https://provider-override.invalid/v1",
    "MISTRAL_BASE_URL": "https://mistral-override.invalid/v1",
    "MISTRAL_API_BASE": "https://mistral-api-override.invalid/v1",
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
# Vibe 3.1.2: vibe/_process_actions.py, vibe/_subagents/_core_adapter.py,
# vibe/_foreign_hooks.py and the legacy built-in task; pinned without importing Vibe.
VIBE_312_DENIED_TOOL_NAMES = (
    "file_system.bash",
    "process.start",
    "process.write",
    "process.output",
    "process.list",
    "process.stop",
    "agents.list",
    "agents.spawn",
    "agents.send_message",
    "agents.interrupt",
    "task",
)
VIBE_312_WORKER_DISABLED_TOOLS = frozenset(VIBE_312_DENIED_TOOL_NAMES[:-1])
VIBE_312_MCP_TOOL_NAMES = (
    "mcp_compchem-memory.memory_get_context",
    "mcp_compchem-tools.run_shell",
)
PROXY_ENV = {
    name: f"{name.lower()}-proxy-sentinel"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
}


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
    stderr_lines: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    state = tmp_path / "stub-state"
    state.mkdir()
    config = tmp_path / "vibe-stub.json"
    config.write_text(json.dumps({
        "exports": [str(path) if path is not None else None for path in exports],
        "stdout_lines": stdout_lines or [[] for _ in exports],
        "state": str(state),
        "linger_after_first": linger_after_first,
        "stderr_lines": stderr_lines or [],
    }))
    binary = tmp_path / "vibe-stub"
    binary.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
config = json.loads(pathlib.Path(__file__).with_suffix(".json").read_text())
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
    "child_names": sorted(os.environ),
    "proxy_env": {name: os.environ.get(name) for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"
    )},
    "stub_config_forwarded": "VIBE_STUB_CONFIG" in os.environ,
}))
source = config["exports"][index]
if source:
    shutil.copy(source, output_dir / "export.json")
print(json.dumps(config["stdout_lines"][index]), flush=True)
for line in config["stderr_lines"]:
    print(line, file=sys.stderr, flush=True)
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
    role: str = "reviewer",
    binary: Path | None = None,
    governance: InvocationGovernance | None = None,
    environment: dict[str, str] | None = None,
    renderer_env: dict[str, str] | None = None,
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
        work_dir=run / "work", role=role, invocation_id=f"{role}-one",
        noninteractive=True,
    )
    for relative, body in native_artifacts(launch_context, []).items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    trust = tmp_path / "trusted_folders.toml"
    trust.write_text(f'trusted = [{json.dumps(str(project.resolve()))}]\nuntrusted = []\n')
    route = ResolvedRoute(
        "vibe", "selected-model", "family", "vibe", role, "test"
    )
    capabilities = ["native_read", "native_glob", "native_grep"]
    if role == "worker":
        capabilities += [
            "native_edit",
            "mcp:compchem-memory.memory_get_context",
            "mcp:compchem-memory.memory_search",
            "mcp:compchem-memory.memory_get_run_history",
        ]
    evidence = {
        "accepted": True, "trusted": True, "stale": False, "identity": "pending",
        "discovery_dir": {
            "scope": "launch-project",
            "configuration": "run-owned project config",
        },
        "capabilities": capabilities,
        "model_isolation": {
            "accepted": True, "model": "selected-model", "identity": "fixture"
        },
    }
    if environment is None:
        environment = {"PATH": os.environ.get("PATH", ""), **PARENT_CLIENT_ENV}
    context = ExecContext(
        route=route, role=role, project_dir=project, run_dir=run,
        control_dir=run / "control", work_dir=run / "work",
        installation_root=ROOT, workspace_root=tmp_path,
        project_identity="project-fixture", python_executable="/venv/bin/python",
        tools_port=8123, launch_argv=(str(binary or "vibe-stub"),),
        role_capabilities={"vibe": {role: evidence}},
        environment=environment,
        renderer_env=renderer_env or {},
        setup={"vibe_trust_path": str(trust)},
        governance=governance,
        timeout_seconds=3,
        invocation_id=f"{role}-one",
    )
    evidence["identity"] = configuration_identity(context)
    return context


def _calls(state: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(state.glob("call-*.json"))]


def _vibe_pre_tool(executor, payload):
    hook = next(
        item for item in tomllib.loads(
            (executor.prepared.invocation_dir / ".vibe" / "hooks.toml").read_text()
        )["hooks"]
        if item["type"] == "pre_tool"
    )
    environment = dict(os.environ)
    environment.update(executor.prepared.env)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(ROOT),
        str(ROOT / "mcp-servers" / "compchem-memory" / "src"),
        str(ROOT / "mcp-servers" / "compchem-tools" / "src"),
    )))
    hook_command = hook["command"].replace("/venv/bin/python", sys.executable)
    return subprocess.run(
        ["sh", "-c", hook_command], input=json.dumps(payload), text=True,
        capture_output=True, timeout=30, env=environment, check=False,
    )


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
    assert disabled == list(READ_ONLY_DISABLED_TOOLS)
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


def _worker_context(tmp_path: Path, **changes) -> ExecContext:
    return _context(tmp_path, role="worker", **changes)


def test_vibe_worker_requires_accepted_current_capability_row(tmp_path):
    context = _worker_context(tmp_path)
    for evidence in (
        {"accepted": False},
        {**context.role_capabilities["vibe"]["worker"], "stale": True},
        {**context.role_capabilities["vibe"]["worker"], "identity": "stale"},
    ):
        unaccepted = replace(context, role_capabilities={"vibe": {"worker": evidence}})
        with pytest.raises(ExecutorRefused, match="lacks accepted worker role capability|identity|stale"):
            executor_for(context.route, "worker", unaccepted)


def test_vibe_accepted_worker_constructs_without_hard_D4_refusal(tmp_path):
    context = _worker_context(tmp_path)

    executor = executor_for(context.route, "worker", context)

    assert isinstance(executor, VibeExecutor)
    assert executor.role == "worker"


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_vibe_disabled_tools_match_role_in_toml_and_argv(tmp_path, role):
    context = _context(tmp_path, role=role)
    expected = set(disabled_tools_for_role(role))
    config = tomllib.loads(native_artifacts(
        LaunchContext(
            root=ROOT, project="project", project_dir=context.project_dir,
            python="/venv/bin/python", tools_port=8123, model="selected-model",
            setup={}, run_dir=context.run_dir, control_dir=context.control_dir,
            work_dir=context.work_dir, role=role, invocation_id=f"{role}-one",
            noninteractive=True,
        ), [],
    )[".vibe/config.toml"].decode())

    assert set(config["disabled_tools"]) == expected
    if role != "worker":
        assert expected == set(READ_ONLY_DISABLED_TOOLS)
    executor = executor_for(context.route, role, context)
    argv = executor._argv("prompt", tmp_path / "out", 8)
    disabled = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--disabled-tools"]
    assert set(disabled) == expected


def test_vibe_worker_retains_only_edit_and_write(tmp_path):
    disabled = set(disabled_tools_for_role("worker"))
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)

    assert disabled == VIBE_312_WORKER_DISABLED_TOOLS
    assert not {"file_system.edit", "file_system.write_file"} & disabled
    config = tomllib.loads(
        (executor.prepared.invocation_dir / ".vibe" / "config.toml").read_text()
    )
    assert set(config["disabled_tools"]) == disabled


def test_vibe_nonworker_roles_retain_full_read_only_deny_set():
    for role in ("reviewer", "helper", "evaluator"):
        assert set(disabled_tools_for_role(role)) == set(READ_ONLY_DISABLED_TOOLS)
        assert VIBE_312_WORKER_DISABLED_TOOLS <= set(disabled_tools_for_role(role))


def _live_native_payload(tool: str, cwd: Path, target: object) -> dict:
    filename = {
        "read": "04-pre_tool-file_system.read_file.json",
        "edit": "07-pre_tool-file_system.edit.json",
        "write": "09-pre_tool-file_system.write_file.json",
    }[tool]
    recorded = json.loads(
        (ROOT / "tests" / "fixtures" / "executors" / "vibe" / "live-p3" / "v1" / filename).read_text()
    )["stdin"]
    payload = json.loads(json.dumps(recorded))
    payload["cwd"] = str(cwd)
    if target is not None:
        payload["tool_input"]["file_path"] = target
    return payload


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_vibe_live_read_payload_follows_role_policy(tmp_path, role):
    context = _context(tmp_path, role=role)
    executor = executor_for(context.route, role, context)

    result = _vibe_pre_tool(
        executor, _live_native_payload("read", context.project_dir, "README.md")
    )

    assert result.returncode == 0
    if role in {"worker", "reviewer"}:
        assert result.stdout == ""
    else:
        assert json.loads(result.stdout)["decision"] == "deny"


@pytest.mark.parametrize("tool", ("read", "edit", "write"))
def test_vibe_live_worker_payloads_allow_work_targets(tmp_path, tool):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)

    result = _vibe_pre_tool(
        executor,
        _live_native_payload(
            tool,
            context.project_dir,
            "README.md" if tool == "read" else str(context.work_dir / "result.txt"),
        ),
    )

    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("tool", ("edit", "write"))
@pytest.mark.parametrize("role", ("reviewer", "helper", "evaluator"))
def test_vibe_live_nonworker_mutation_payloads_denied(tmp_path, role, tool):
    context = _context(tmp_path, role=role)
    executor = executor_for(context.route, role, context)

    result = _vibe_pre_tool(
        executor,
        _live_native_payload(tool, context.project_dir, str(context.work_dir / "result.txt")),
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "deny"


def test_vibe_live_payloads_deny_outside_secret_and_alias_targets(tmp_path):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    secret = context.project_dir / "secret"
    secret.mkdir()
    (secret / "token.txt").write_text("secret", encoding="utf-8")
    (context.project_dir / "alias").symlink_to(secret, target_is_directory=True)
    (context.project_dir / "alias.env").symlink_to(secret / "token.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("outside", encoding="utf-8")
    (context.project_dir / "link-out").symlink_to(outside, target_is_directory=True)

    denied_targets = (
        str(outside / "loot.txt"),
        ".env",
        "alias.env",
        "link-out/loot.txt",
    )
    for tool in ("read", "edit", "write"):
        for target in denied_targets:
            result = _vibe_pre_tool(
                executor, _live_native_payload(tool, context.project_dir, target)
            )
            assert result.returncode == 0
            assert json.loads(result.stdout)["decision"] == "deny", (tool, target, result.stdout)


@pytest.mark.parametrize("tool", ("read", "edit", "write"))
def test_vibe_path_alone_is_not_a_native_file_target(tmp_path, tool):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    payload = _live_native_payload(tool, context.project_dir, None)
    payload["tool_input"] = {"path": "work/result.txt"}

    result = _vibe_pre_tool(executor, payload)

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "deny"


def test_vibe_grep_decodes_search_scope_and_denies_unknown_glob(tmp_path):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "executors" / "vibe" / "synthetic" / "native-tools.json").read_text()
    )["grep"]
    fixture["cwd"] = str(context.project_dir)
    fixture["tool_input"]["path"] = "."

    for payload in (fixture, {**fixture, "tool_input": {"pattern": "needle"}}):
        allowed = _vibe_pre_tool(executor, payload)
        assert allowed.returncode == 0
        assert allowed.stdout == ""

    for target in ("../outside", "", 17):
        denied = _vibe_pre_tool(
            executor, {**fixture, "tool_input": {"pattern": "needle", "path": target}}
        )
        assert denied.returncode == 0
        assert json.loads(denied.stdout)["decision"] == "deny"

    glob = _vibe_pre_tool(executor, {**fixture, "tool_name": "glob"})
    assert glob.returncode == 0
    assert json.loads(glob.stdout)["decision"] == "deny"


def test_vibe_worker_work_edit_allowed_and_outside_edit_denied_by_pre_tool(tmp_path):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "executors" / "vibe" / "synthetic" / "native-tools.json").read_text()
    )
    secret = context.project_dir / "secret"
    secret.mkdir()
    (secret / "token.txt").write_text("synthetic", encoding="utf-8")
    (context.project_dir / "alias").symlink_to(secret, target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("synthetic", encoding="utf-8")
    (outside / "outside.txt").write_text("synthetic", encoding="utf-8")
    (context.project_dir / "link-out").symlink_to(outside, target_is_directory=True)

    def payload(tool, path):
        value = json.loads(json.dumps(fixture[tool]))
        value["cwd"] = str(context.project_dir)
        value["tool_input"]["file_path"] = str(path)
        return value

    for tool in ("edit", "write"):
        allowed = _vibe_pre_tool(executor, payload(tool, context.work_dir / "new.txt"))
        assert allowed.returncode == 0
        assert allowed.stdout == ""
    denied_paths = (
        context.project_dir / "project.txt",
        context.project_dir / ".env",
        context.project_dir / "alias" / "token.txt",
        context.project_dir / "conf.env.d" / "nested" / "key",
        context.project_dir / "link-out" / ".." / "loot.txt",
        outside / "outside.txt",
    )
    for tool in ("edit", "write"):
        for path in denied_paths:
            denied = _vibe_pre_tool(executor, payload(tool, path))
            assert denied.returncode == 0
            assert json.loads(denied.stdout)["decision"] == "deny"
    assert not (context.work_dir / "new.txt").exists()
    assert (outside / "outside.txt").read_text() == "synthetic"


@pytest.mark.parametrize("tool", ("read", "edit", "write"))
@pytest.mark.parametrize("target", (None, "", 17))
def test_vibe_worker_malformed_or_missing_native_file_target_denied(tmp_path, tool, target):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "executors" / "vibe" / "synthetic" / "native-tools.json").read_text()
    )[tool]
    fixture["cwd"] = str(context.project_dir)
    key = "file_path"
    if target is None:
        del fixture["tool_input"][key]
    else:
        fixture["tool_input"][key] = target

    denied = _vibe_pre_tool(executor, fixture)

    assert denied.returncode == 0
    assert json.loads(denied.stdout)["decision"] == "deny"


@pytest.mark.parametrize("tool", ("read", "edit", "write"))
def test_vibe_worker_ambiguous_native_file_target_denied(tmp_path, tool):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "executors" / "vibe" / "synthetic" / "native-tools.json").read_text()
    )[tool]
    fixture["cwd"] = str(context.project_dir)
    key = "file_path"
    alternate = "path"
    fixture["tool_input"][key] = str(context.work_dir / "allowed.txt")
    fixture["tool_input"][alternate] = str(context.project_dir / "project.txt")

    denied = _vibe_pre_tool(executor, fixture)

    assert denied.returncode == 0
    assert json.loads(denied.stdout)["decision"] == "deny"


def test_vibe_worker_shell_process_delegation_and_unapproved_mcp_denied(tmp_path):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    base = {
        "session_id": "synthetic-vibe-worker",
        "parent_session_id": None,
        "cwd": str(executor.prepared.discovery_dir),
        "hook_event_name": "pre_tool",
        "tool_call_id": "synthetic-denied",
        "tool_input": {},
    }
    for tool in (*VIBE_312_DENIED_TOOL_NAMES, *VIBE_312_MCP_TOOL_NAMES):
        denied = _vibe_pre_tool(executor, {**base, "tool_name": tool})
        assert denied.returncode == 0
        assert json.loads(denied.stdout)["decision"] == "deny"


def test_vibe_312_disabled_tool_inventory_is_versioned_and_complete():
    # Static inventory from Vibe 3.1.2 _process_actions.py and _subagents/_core_adapter.py.
    assert VIBE_312_WORKER_DISABLED_TOOLS == {
        "file_system.bash",
        "process.start", "process.write", "process.output", "process.list", "process.stop",
        "agents.list", "agents.spawn", "agents.send_message", "agents.interrupt",
    }


def test_vibe_worker_strict_hook_failure_fails_closed_before_spawn(tmp_path, monkeypatch):
    context = _worker_context(tmp_path)
    executor = executor_for(context.route, "worker", context)
    calls = []
    monkeypatch.setattr(
        "campaign_runner.executors.vibe.run_process",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "executors" / "vibe" / "synthetic" / "native-tools.json").read_text()
    )["write"]
    fixture["cwd"] = str(context.project_dir)
    fixture["tool_input"]["file_path"] = str(context.work_dir / "new.txt")
    policy = executor.prepared.invocation_dir / "policy.json"
    policy.chmod(0o644)
    policy.write_text("{", encoding="utf-8")
    policy.chmod(0o444)
    denied = _vibe_pre_tool(executor, fixture)
    assert denied.returncode == 0
    assert json.loads(denied.stdout)["decision"] == "deny"
    assert calls == []
    assert not (context.work_dir / "new.txt").exists()


def test_vibe_direct_validate_accepts_four_roles_and_rejects_other(tmp_path):
    for role in ("worker", "reviewer", "helper", "evaluator"):
        context = _context(tmp_path / role, role=role)
        prepared = prepare_launch(context)
        executor = VibeExecutor(prepared=prepared, model="selected-model", role=role, max_turns=8)
        assert executor.role == role
    context = _worker_context(tmp_path / "other")
    prepared = prepare_launch(context)
    with pytest.raises(ExecutorRefused, match="does not implement the other role"):
        VibeExecutor(prepared=prepared, model="selected-model", role="other", max_turns=8)


def test_vibe_real_export_selected_model_and_fallback_notice():
    accepted = parse_vibe_export(json.dumps(_real_export()))
    fallback = parse_vibe_export(json.dumps(_fallback_export()))
    assert accepted.reported_model == "selected-model"
    assert accepted.outcome.kind == "success"
    assert fallback.reported_model == "Mistral Medium 3.5"
    assert fallback.reported_model != accepted.reported_model


def test_vibe_child_env_allows_only_mistral_account_process_basics(tmp_path):
    binary, _, state = _stub(tmp_path, [None])
    context = _context(
        tmp_path,
        binary=binary,
        governance=_governance(tmp_path),
        environment={
            "HOME": str(tmp_path / "member-home"),
            "USER": "member",
            "LOGNAME": "member",
            "SHELL": "/bin/zsh",
            "TMPDIR": str(tmp_path),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "PATH": os.environ.get("PATH", ""),
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "NODE_EXTRA_CA_CERTS": "/etc/ssl/cert.pem",
            **PROXY_ENV,
            **PARENT_CLIENT_ENV,
            "VIBE_STUB_CONFIG": "must-not-reach-child",
        },
    )
    executor = executor_for(context.route, "reviewer", context)

    result = executor.run("review", context.project_dir, tmp_path / "attempt")

    (call,) = _calls(state)
    assert call["client_env"] == {}
    assert set(call["child_names"]) - set(executor.prepared.env) <= {
        "__CF_USER_TEXT_ENCODING"
    }
    assert call["proxy_env"] == PROXY_ENV
    assert not call["stub_config_forwarded"]
    assert "VIBE_STUB_CONFIG" not in executor.prepared.env
    assert "VIBE_HOME" not in executor.prepared.env
    assert result.outcome.kind == "failure"


def test_vibe_renderer_env_cannot_inject_provider_or_endpoint(tmp_path):
    binary, _, state = _stub(tmp_path, [None])
    context = _context(
        tmp_path,
        binary=binary,
        governance=_governance(tmp_path),
        renderer_env={
            "VENDOR_SPECIFIC_API_KEY": "renderer-provider-key",
            "MISTRAL_API_KEY": "renderer-mistral-key",
            "OPENAI_BASE_URL": "https://renderer-provider.invalid/v1",
            "MISTRAL_API_BASE": "https://renderer-mistral.invalid/v1",
            "MISTRAL_PROVIDER": "renderer-provider",
            **PROXY_ENV,
        },
    )
    executor = executor_for(context.route, "reviewer", context)

    executor.run("review", context.project_dir, tmp_path / "attempt")

    (call,) = _calls(state)
    assert call["client_env"] == {}
    assert call["proxy_env"] == PROXY_ENV
    assert "OPENAI_BASE_URL" not in call["child_names"]
    assert "MISTRAL_API_BASE" not in call["child_names"]
    assert "MISTRAL_PROVIDER" not in call["child_names"]


def test_vibe_missing_or_mismatched_effective_model_is_not_accepted(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps(_real_export(model=None)))
    binary, _, state = _stub(tmp_path, [export])
    context = _context(tmp_path, binary=binary, governance=_governance(tmp_path))

    result = executor_for(context.route, "reviewer", context).run(
        "review", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "failure"
    assert "model_unverified" in [item["kind"] for item in result.errors]
    assert result.session_identity is None
    assert result.resume_artifact is None
    assert len(_calls(state)) == 1
    assert len((tmp_path / "usage-ledger.jsonl").read_text().splitlines()) == 1


def test_vibe_successful_process_fallback_is_not_accepted(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps(_fallback_export()))
    binary, _, state = _stub(tmp_path, [export])
    context = _context(tmp_path, binary=binary, governance=_governance(tmp_path))

    result = executor_for(context.route, "reviewer", context).run(
        "review", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "model_mismatch"
    assert result.session_identity is None
    assert result.resume_artifact is None
    assert "model_mismatch" in [item["kind"] for item in result.errors]


def test_vibe_missing_mistral_credentials_surfaces_own_setup_guidance(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps(_real_export(outcome="error", exit_code=1)))
    binary, _, state = _stub(
        tmp_path,
        [export],
        stderr_lines=[
            "Authentication failed: Mistral API key not found in Vibe configuration or keyring."
        ],
    )
    context = _context(tmp_path, binary=binary, governance=_governance(tmp_path))

    result = executor_for(context.route, "reviewer", context).run(
        "review", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "failure"
    guidance = [item for item in result.errors if item["kind"] == "mistral_setup_required"]
    assert len(guidance) == 1
    assert "run Vibe's own setup once" in guidance[0]["message"]
    assert len(_calls(state)) == 1
    assert len((tmp_path / "usage-ledger.jsonl").read_text().splitlines()) == 1


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
        stdout_lines=[[{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "REVIEW-OK"}],
        }]],
    )
    governance = _governance(tmp_path)
    context = _context(
        tmp_path, binary=binary, governance=governance
    )

    result = executor_for(context.route, "reviewer", context).run(
        "review", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == expected
    if expected != "success":
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
    if expected == "success":
        assert json.loads(rows[0])["session_identity"]["session_id"] == "vibe-session"
    else:
        assert json.loads(rows[0])["session_identity"] is None
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
    context = _context(tmp_path, binary=binary, governance=_governance(tmp_path))
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
        tmp_path, binary=binary, governance=governance
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


@pytest.mark.parametrize("resumed_payload", (_real_export(model=None), _fallback_export()))
def test_vibe_resumed_unaccepted_model_suppresses_session_and_receipt_identity(
    tmp_path, resumed_payload
):
    first_export = tmp_path / "first.json"
    first_export.write_text(json.dumps(_real_export()))
    resumed_export = tmp_path / "resumed.json"
    resumed_export.write_text(json.dumps(resumed_payload))
    binary, _, state = _stub(tmp_path, [first_export, resumed_export])
    first_context = _context(
        tmp_path, binary=binary, governance=_governance(tmp_path)
    )
    first_executor = executor_for(first_context.route, "reviewer", first_context)
    first = first_executor.run(
        "first", first_context.project_dir, tmp_path / "attempt-1"
    )
    resume_context = replace(
        first_context,
        resume_from=first_executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    second = executor_for(resume_context.route, "reviewer", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    expected_kind = (
        "model_mismatch"
        if resumed_payload["config"]["active_model"] == ""
        else "failure"
    )
    assert second.outcome.kind == expected_kind
    assert second.session_id is None
    assert second.session_identity is None
    assert second.resume_artifact is None
    assert len(_calls(state)) == 2
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 2
    assert rows[0]["session_identity"]["session_id"] == "vibe-session"
    assert rows[1]["session_identity"] is None
    assert rows[1]["usage"]["input_tokens"] == 135962
    assert rows[1]["outcome"]["kind"] == expected_kind


def test_vibe_resumed_wrong_session_precedes_bad_model(tmp_path):
    first_export = tmp_path / "first.json"
    first_export.write_text(json.dumps(_real_export()))
    resumed_export = tmp_path / "resumed.json"
    resumed_payload = _fallback_export()
    resumed_payload["session_id"] = "other-session"
    resumed_export.write_text(json.dumps(resumed_payload))
    binary, _, state = _stub(tmp_path, [first_export, resumed_export])
    first_context = _context(
        tmp_path, binary=binary, governance=_governance(tmp_path)
    )
    first_executor = executor_for(first_context.route, "reviewer", first_context)
    first = first_executor.run(
        "first", first_context.project_dir, tmp_path / "attempt-1"
    )
    resume_context = replace(
        first_context,
        resume_from=first_executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    second = executor_for(resume_context.route, "reviewer", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    assert second.outcome.kind == "refused"
    assert second.session_id is None
    assert second.session_identity is None
    assert second.resume_artifact is None
    assert "resume_identity_mismatch" in [item["kind"] for item in second.errors]
    assert "model_mismatch" in [item["kind"] for item in second.errors]
    assert len(_calls(state)) == 2
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 2
    assert rows[1]["session_identity"] is None
    assert rows[1]["usage"]["input_tokens"] == 135962
    assert rows[1]["outcome"]["kind"] == "refused"


def test_vibe_resume_artifact_round_trip_and_mismatch_refusal(tmp_path):
    first_export = tmp_path / "first.json"
    first_export.write_text(json.dumps(_real_export()))
    resumed_export = tmp_path / "resumed.json"
    resumed_export.write_text(json.dumps(_real_export()))
    binary, config, state = _stub(tmp_path, [first_export, resumed_export])
    first_context = _context(tmp_path, binary=binary)
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
    context = _context(tmp_path, binary=binary)
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
    context = _context(tmp_path, binary=binary)
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


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
@pytest.mark.parametrize("damage", ("hook", "mcp", "tool", "model"))
def test_vibe_refuses_each_project_native_drift(tmp_path, role, damage):
    context = _context(tmp_path, role=role)
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
    context.role_capabilities["vibe"][role]["identity"] = configuration_identity(context)

    with pytest.raises(ExecutorRefused, match="project.*Vibe|Vibe.*project|selected model"):
        executor_for(context.route, role, context)


def test_vibe_memory_released_once_after_worker_exit(tmp_path, monkeypatch):
    """Release is idempotent independently of worker construction."""
    prepared = type("Prepared", (), {"memory_service_handle": object()})()
    released = []
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda value: released.append(value) or True,
    )

    VibeExecutor._release_prepared_memory_service(prepared)
    VibeExecutor._release_prepared_memory_service(prepared)

    assert len(released) == 1
    assert prepared.memory_service_handle is None


def test_vibe_memory_released_after_prepare_or_validation_refusal(tmp_path, monkeypatch):
    from campaign_runner.launch import prepare_launch

    token_file = tmp_path / "managed-token"
    token_file.write_text("service-bearer-sentinel\n", encoding="utf-8")
    token_file.chmod(0o644)
    handle = type(
        "Handle",
        (),
        {
            "route": lambda self: {
                "transport": "http",
                "url": "http://127.0.0.1:43101/mcp",
                "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
                "token_file": str(token_file),
            }
        },
    )()
    released = []
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.ensure_memory_service",
        lambda context, **kwargs: handle,
    )
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda value: released.append(value) or True,
    )
    base = _context(tmp_path)
    worker = "mcp:compchem-memory.memory_get_context"
    context = replace(
        base,
        role="worker",
        memory_service={"transport": "managed"},
        role_capabilities={
            "vibe": {
                "worker": {
                    **base.role_capabilities["vibe"]["reviewer"],
                    "capabilities": [
                        "native_read", "native_glob", "native_grep",
                        "native_edit", worker,
                        "mcp:compchem-memory.memory_search",
                        "mcp:compchem-memory.memory_get_run_history",
                    ],
                    "vibe_memory": True,
                }
            }
        },
    )
    context = replace(context, role_capabilities={
        "vibe": {"worker": {**context.role_capabilities["vibe"]["worker"],
                            "identity": configuration_identity(context)}}})

    with pytest.raises(ExecutorRefused, match="0600"):
        prepare_launch(context)

    assert released == [handle]

    token_file.chmod(0o600)
    launch = prepare_launch(context)
    prepared = replace(launch, memory_service_handle=handle)
    released.clear()
    with pytest.raises(ExecutorRefused, match="Vibe project file.*differs"):
        VibeExecutor(
            prepared=prepared,
            model="selected-model",
            role="worker",
            max_turns=8,
        )
    assert released == [handle]


def test_vibe_memory_reviewer_lease_survives_repairs_then_close_releases_once(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    executor = executor_for(context.route, "reviewer", context)
    handle = object()
    object.__setattr__(executor.prepared, "memory_service_handle", handle)
    released = []
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda value: released.append(value) or True,
    )
    monkeypatch.setattr(VibeExecutor, "_run", lambda self, *args, **kwargs: object())

    executor.run("review", tmp_path, tmp_path / "attempt", stem="reviewer")
    executor.run("review", tmp_path, tmp_path / "attempt", stem="reviewer_retry_1")
    assert released == []
    assert executor.prepared.memory_service_handle is handle

    executor.close()
    assert released == [handle]
    assert executor.prepared.memory_service_handle is None

    executor.close()
    assert released == [handle]
