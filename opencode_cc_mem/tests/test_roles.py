from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from campaign_runner import roles
from campaign_runner.launch import ROLE_CONFIG_NAMES


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("role", ("campaign-worker", "campaign-reviewer"))
def test_render_opencode_byte_identical(role):
    assert roles.render_opencode(role) == (ROOT / ".opencode" / "agents" / f"{role}.md").read_bytes()


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
        ("read_deny_patterns", ["**/private/**"]),
        ("edit_roots", []),
        ("mcp_tool_allowlist", []),
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
