import json
from pathlib import Path

from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_common import harness_dir, policy_files


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
