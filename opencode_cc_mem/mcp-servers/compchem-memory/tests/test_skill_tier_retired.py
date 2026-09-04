"""Skill-tier retirement regression tests (2026-09).

Locks in the Phase-2 contract:
  - no skill tier anywhere: boot context, scan_headers tiers, search results
  - memory_promote MCP tool is gone
  - RULES_DIR resolves from MAGNOLIA_RULES_DIR env
  - elevated rules land in the rules dir with the rules/ frontmatter shape
"""

import json
from pathlib import Path

from compchem_memory.context_assembly import assemble_context, allocate_budget
from compchem_memory.storage import ensure_project_store


def test_no_memory_promote_tool():
    import asyncio

    from compchem_memory import server

    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "memory_promote" not in tools
    # the surviving elevation path
    assert "memory_review_promotions" in tools
    assert "memory_apply_promotions" in tools


def test_assemble_context_has_no_skill_section(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    (pd / ".magnolia" / "GOAL.md").write_text("# Goal\nx\n")
    result = assemble_context(
        task_description="anything", project_dir=str(pd), token_budget=4000
    )
    assert "[SKILL:" not in result.content
    assert all(s["tier"] != "skill" for s in result.sources)


def test_budget_allocation_has_no_skill_floor():
    a = allocate_budget(10000)
    assert not hasattr(a, "skill_budget")
    # the freed 30% goes to the project tier
    assert a.project_budget == min(int(10000 * 0.70), 12000)


def test_rules_dir_env_override(tmp_path, monkeypatch):
    import importlib
    import compchem_memory.server as server

    monkeypatch.setenv("MAGNOLIA_RULES_DIR", str(tmp_path / "myrules"))
    try:
        importlib.reload(server)
        assert server.RULES_DIR == tmp_path / "myrules"
    finally:
        monkeypatch.delenv("MAGNOLIA_RULES_DIR")
        importlib.reload(server)


def test_apply_promotions_writes_to_rules_dir(tmp_path):
    from compchem_memory.promotion import propose_promotions, apply_promotions

    store = tmp_path / ".magnolia"
    entries = store / "entries"
    entries.mkdir(parents=True)
    (entries / "a.md").write_text(
        "---\ntitle: Alpha\nobserved_in_sessions: [s1, s2, s3]\ntype: note\n"
        "confidence: 0.9\n---\nBody text.\n"
    )
    rules = tmp_path / "rules"
    rules.mkdir()

    def _approve_all(entry, k):
        return {"approve": True}

    propose_promotions(
        str(store), rules_dir=str(rules), judge=_approve_all,
        drafter=lambda e: None,  # fall back to faithful copy draft
        checker=lambda d, r: {"status": "ok"},
    )
    res = apply_promotions(str(store), str(rules), accept=[0])
    assert res["applied"] == 1
    rule_file = rules / "alpha.md"
    assert rule_file.exists()
    text = rule_file.read_text()
    # rules/ frontmatter shape
    assert text.startswith("---")
    assert "name:" in text.split("---")[1]
    assert "description:" in text.split("---")[1]
    # source entry archived (backed up + removed)
    assert not (entries / "a.md").exists()
