from pathlib import Path

import pytest

from campaign_runner import roles


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


def test_all_registry_roles_have_policy():
    policies = roles.load_policies()

    assert set(policies) == {"campaign-worker", "campaign-reviewer", "helper", "evaluator"}
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

