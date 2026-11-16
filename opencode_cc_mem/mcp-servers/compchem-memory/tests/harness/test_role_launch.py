import json
import os
import subprocess
from pathlib import Path

import pytest

from compchem_memory.harness import RENDERERS
from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_common import (
    _required_pre_tool_command,
    harness_dir,
    policy_files,
)


def test_noninteractive_policy_is_run_owned_and_required(tmp_path):
    project = tmp_path / "project"
    control = tmp_path / "run" / "control"
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=project,
        python="/venv/bin/python",
        tools_port=8123,
        model="selected-model",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=control,
        work_dir=tmp_path / "run" / "work",
        role="helper",
        invocation_id="helper-1",
        noninteractive=True,
        role_policy={"role": "helper", "mcp_tool_allowlist": []},
    )

    files, env = policy_files(NeutralConfig([], frozenset({"bash"})), context, "claude")
    expected_dir = control / "executors" / "helper-1"
    policy_path = expected_dir / "policy.json"

    assert harness_dir(context, "claude") == expected_dir
    assert set(files) == {policy_path}
    assert json.loads(files[policy_path]) == {
        "deny_tools": ["bash"],
        "mcp_tool_allowlist": [],
        "role": "helper",
    }
    assert env == {
        "MAGNOLIA_HOOK_POLICY": str(policy_path.resolve()),
        "MAGNOLIA_HOOK_POLICY_REQUIRED": "1",
        "MAGNOLIA_DOORBELL": "0",
    }


def test_interactive_policy_location_and_environment_stay_compatible(make_render):
    neutral, context = make_render()

    files, env = policy_files(neutral, context, "codex")

    expected = context.project_dir / ".magnolia" / "harness" / "codex" / "policy.json"
    assert set(files) == {expected}
    assert env == {"MAGNOLIA_HOOK_POLICY": str(expected)}


@pytest.mark.parametrize("missing", ("control", "invocation"))
def test_noninteractive_policy_requires_run_owned_location(tmp_path, missing):
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=tmp_path / "project",
        python="/venv/bin/python",
        tools_port=8123,
        model=None,
        setup={},
        control_dir=None if missing == "control" else tmp_path / "run" / "control",
        invocation_id=None if missing == "invocation" else "one",
        noninteractive=True,
    )

    with pytest.raises(ValueError, match="control_dir and invocation_id"):
        harness_dir(context, "claude")


def test_role_policy_cannot_weaken_neutral_tool_denial(tmp_path):
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=tmp_path / "project",
        python="/venv/bin/python",
        tools_port=8123,
        model=None,
        setup={},
        control_dir=tmp_path / "run" / "control",
        invocation_id="one",
        noninteractive=True,
        role_policy={"role": "helper", "deny_tools": []},
    )

    files, _ = policy_files(
        NeutralConfig([], frozenset({"bash", "shell"})), context, "claude"
    )

    assert json.loads(next(iter(files.values())))["deny_tools"] == ["bash", "shell"]


@pytest.mark.parametrize(
    ("client", "hook_file", "hook_marker"),
    (
        ("claude", "settings.json", "PreToolUse"),
        ("codex", ".codex/hooks.json", "PreToolUse"),
        ("vibe", ".vibe/hooks.toml", "magnolia-pre-tool"),
    ),
)
def test_noninteractive_role_env_all_three_clients(
    tmp_path, client, hook_file, hook_marker
):
    project = tmp_path / "project"
    control = tmp_path / "run" / "control"
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=project,
        python="/venv/bin/python",
        tools_port=8123,
        model="selected-model",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=control,
        work_dir=tmp_path / "run" / "work",
        role="reviewer",
        invocation_id=f"{client}-reviewer",
        noninteractive=True,
        role_policy={"role": "reviewer", "mcp_tool_allowlist": []},
    )

    launch = RENDERERS[client](NeutralConfig([], frozenset({"bash"})), context)

    policy_path = control / "executors" / f"{client}-reviewer" / "policy.json"
    assert launch.env["MAGNOLIA_HOOK_POLICY"] == str(policy_path.resolve())
    assert launch.env["MAGNOLIA_HOOK_POLICY_REQUIRED"] == "1"
    assert launch.env["MAGNOLIA_DOORBELL"] == "0"
    if client == "claude":
        rendered_hook = launch.files[policy_path.parent / hook_file]
    else:
        rendered_hook = launch.files[project / hook_file]
    text = rendered_hook.decode("utf-8")
    assert hook_marker in text
    assert "compchem_memory.hook" in text
    assert not any(
        forbidden in " ".join(launch.argv) + text
        for forbidden in ("--trust", "--dangerously", "bypass")
    )


def _pre_tool_command(launch, project, policy_path, client):
    if client == "claude":
        settings = json.loads(launch.files[policy_path.parent / "settings.json"])
        return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    if client == "codex":
        hooks = json.loads(launch.files[project / ".codex/hooks.json"])
        return hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    import tomllib

    hooks = tomllib.loads(launch.files[project / ".vibe/hooks.toml"].decode())["hooks"]
    return next(hook["command"] for hook in hooks if hook["name"] == "magnolia-pre-tool")


@pytest.mark.parametrize("client", ("claude", "codex", "vibe"))
@pytest.mark.parametrize("failure", ("missing-interpreter", "import-failure"))
def test_noninteractive_pre_tool_hook_failure_blocks(
    tmp_path, client, failure
):
    project = tmp_path / "project"
    control = tmp_path / "run" / "control"
    interpreter = tmp_path / "missing-python"
    if failure == "import-failure":
        interpreter.write_text(
            "#!/bin/sh\necho 'No module named compchem_memory' >&2\nexit 1\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=project,
        python=str(interpreter),
        tools_port=8123,
        model="selected-model",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=control,
        work_dir=tmp_path / "run" / "work",
        role="reviewer",
        invocation_id=f"{client}-reviewer",
        noninteractive=True,
        role_policy={"role": "reviewer", "mcp_tool_allowlist": []},
    )
    launch = RENDERERS[client](NeutralConfig([], frozenset({"bash"})), context)
    policy_path = control / "executors" / f"{client}-reviewer" / "policy.json"
    command = _pre_tool_command(launch, project, policy_path, client)
    env = dict(os.environ)
    env.update(launch.env)

    result = subprocess.run(
        ["sh", "-c", command],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )

    payload = json.loads(result.stdout)
    if client == "vibe":
        assert payload["decision"] == "deny"
    else:
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result.returncode == 0


def test_noninteractive_pre_tool_hook_timeout_blocks():
    command = _required_pre_tool_command("sleep 5", "claude", timeout_s=1)

    result = subprocess.run(
        ["sh", "-c", command],
        input="{}", capture_output=True, text=True, timeout=4,
    )

    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result.returncode == 0
