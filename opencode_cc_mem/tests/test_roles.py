import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from campaign_runner import roles
from campaign_runner.launch import ROLE_CONFIG_NAMES


ROOT = Path(__file__).parents[1]
BASE_OPENCODE_SHA256 = {
    "campaign-worker": "7ba0074627cd59d1cf0e2845430642b516540a8466844240d08b6ce0f6727af8",
    "campaign-reviewer": "db7bfef5aecfd5387d9febf3a7cd041705435ec8e687d68076d91efc641bf20d",
}


@pytest.mark.parametrize("role", ("campaign-worker", "campaign-reviewer"))
def test_render_opencode_byte_identical(role):
    assert roles.render_opencode(role) == (ROOT / ".opencode" / "agents" / f"{role}.md").read_bytes()


@pytest.mark.parametrize("role", ("campaign-worker", "campaign-reviewer"))
def test_render_opencode_preserves_base_bytes(role):
    assert hashlib.sha256(roles.render_opencode(role)).hexdigest() == (
        BASE_OPENCODE_SHA256[role]
    )


def test_role_generation_check_detects_drift(tmp_path):
    target = tmp_path / "agents"
    roles.write_opencode(target)
    assert roles.check_opencode(target) == []

    (target / "campaign-worker.md").write_text("drift\n", encoding="utf-8")
    assert roles.check_opencode(target) == ["campaign-worker.md"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("default_cap", 31),
        ("canonical_read_roots", []),
        (
            "read_deny_patterns",
            ["**/private/**", "**/.magnolia/services/**"],
        ),
        ("edit_roots", []),
    ),
)
def test_role_fields_drive_opencode_output_and_check(field, value, tmp_path):
    config = tmp_path / "agents"
    shutil.copytree(ROOT / "agents", config)
    target = tmp_path / "generated"
    roles.write_opencode(target, config)
    worker = config / "campaign-worker.yaml"
    payload = yaml.safe_load(worker.read_text(encoding="utf-8"))
    payload[field] = value
    worker.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    assert roles.render_opencode("campaign-worker", config) != (
        target / "campaign-worker.md"
    ).read_bytes()
    assert roles.check_opencode(target, config) == ["campaign-worker.md"]


def test_check_opencode_cli_exit_code(tmp_path):
    target = tmp_path / "agents"
    roles.write_opencode(target)
    (target / "campaign-reviewer.md").write_text("drift\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "campaign_runner.roles",
            "--check-opencode",
            str(target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "campaign-reviewer.md" in result.stderr


def test_all_registry_roles_have_policy():
    policies = roles.load_policies()

    assert set(policies) == {"campaign-worker", "campaign-reviewer", "helper", "evaluator"}
    assert set(ROLE_CONFIG_NAMES.values()) == set(policies)
    assert policies["campaign-worker"].default_cap == 30
    assert policies["campaign-reviewer"].default_cap == 8
    assert policies["helper"].default_cap == 40
    assert policies["evaluator"].default_cap == 1
    assert policies["campaign-worker"].mcp_tool_allowlist == (
        "mcp:compchem-memory.memory_get_context",
        "mcp:compchem-memory.memory_search",
        "mcp:compchem-memory.memory_get_run_history",
    )
    assert policies["campaign-reviewer"].edit_roots == ()
    assert policies["helper"].delegation_policy == "deny"
    assert policies["evaluator"].allowed_native_capabilities == ()


def test_worker_exact_three_memory_reads_reviewer_none():
    policies = roles.load_policies()

    assert policies["campaign-worker"].mcp_tool_allowlist == (
        "mcp:compchem-memory.memory_get_context",
        "mcp:compchem-memory.memory_search",
        "mcp:compchem-memory.memory_get_run_history",
    )
    assert policies["campaign-reviewer"].mcp_tool_allowlist == ()


@pytest.mark.parametrize("role", ("campaign-worker", "campaign-reviewer"))
def test_service_state_deny_covers_opencode_read_glob_and_grep(role):
    policy = roles.load_role(role)

    assert "**/.magnolia/services/**" in policy.read_deny_patterns
    rendered = yaml.safe_load(
        roles.render_opencode(role).decode("utf-8").split("---")[1]
    )
    capabilities = set(policy.allowed_native_capabilities)
    actions = {"read"}
    if "native_glob" in capabilities:
        actions.add("glob")
    if "native_grep" in capabilities:
        actions.add("grep")
    expected_resources = roles._opencode_read_denials(
        tuple(
            pattern
            for pattern in policy.read_deny_patterns
            if pattern not in policy.opencode_omitted_read_deny_patterns
        )
    )
    for action in actions:
        denied = {
            rule["resource"]
            for rule in rendered["permissions"]
            if rule["action"] == action and rule["effect"] == "deny"
        }
        assert set(expected_resources) <= denied


@pytest.mark.parametrize(
    "allowlist",
    ([], ["mcp:compchem-memory.memory_not_a_real_tool"]),
)
def test_required_memory_role_refuses_empty_or_unknown_allowlist(tmp_path, allowlist):
    config = tmp_path / "agents"
    shutil.copytree(ROOT / "agents", config)
    worker = config / "campaign-worker.yaml"
    payload = yaml.safe_load(worker.read_text(encoding="utf-8"))
    payload["mcp_tool_allowlist"] = allowlist
    worker.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="memory tool allowlist"):
        roles.load_role("campaign-worker", config)
