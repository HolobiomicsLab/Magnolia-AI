"""Regression tests protecting the closed cybernetics loops listed in
docs/cybernetics_assessment.md §1 and §2. These must keep passing.
"""

import json
from pathlib import Path

import pytest
import yaml

from compchem_memory.context_assembly import assemble_context
from compchem_memory.tiers.project import ProjectManager
from compchem_memory.learning.consolidator import consolidate_tier
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def test_1_4_anti_windup_budget_respected(project_dir, tmp_path):
    """§1.4: assemble_context must respect the token budget."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    result = assemble_context(
        task_description="test budget",
        project_dir=str(project_dir),
        skills_dir=str(skills_dir),
        token_budget=4000,
    )
    assert result.tokens_used <= 4000 * 1.05, (
        f"assemble_context exceeded budget: {result.tokens_used} > 4000"
    )


def test_2_2_goal_loaded_first(project_dir, tmp_path):
    """§2.2: GOAL.md is loaded before other tiers."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    goal_path = project_dir / ".magnolia" / "GOAL.md"
    goal_path.write_text("# Goal\nFind the best 10-mer peptide.\n")

    result = assemble_context(
        task_description="design",
        project_dir=str(project_dir),
        skills_dir=str(skills_dir),
        token_budget=4000,
    )
    assert "Find the best 10-mer peptide" in result.content


def test_2_3_consolidator_enforces_max_entries(project_dir):
    """§2.3: consolidator archives lowest-confidence entries above max_entries."""
    entries_dir = project_dir / ".magnolia" / "entries"
    for i in range(5):
        f = entries_dir / f"entry_{i:02d}.md"
        confidence = 0.3 + i * 0.1
        f.write_text(
            f"---\nname: e{i}\ntitle: Entry {i}\nconfidence: {confidence}\n"
            f"tags: []\nobservation_count: 1\ntype: note\n---\n\nBody\n"
        )

    report = consolidate_tier(
        tier="project",
        base_dir=str(project_dir),
        stale_days=9999,
        max_entries=3,
    )

    archive = project_dir / ".magnolia" / "archive"
    assert archive.exists()
    archived_count = len(list(archive.glob("*.md")))
    assert archived_count >= 2, f"expected ≥2 archived, got {archived_count}"


def test_2_4_negative_confidence_feedback(project_dir):
    """§2.4: decrement_confidence_for_tool reduces confidence on failed runs."""
    pm = ProjectManager(Path.home() / ".magnolia")
    entries_dir = project_dir / ".magnolia" / "entries"
    entry = entries_dir / "haddock_success.md"
    entry.write_text(
        "---\nname: haddock_success\ntitle: Haddock works\nconfidence: 0.9\n"
        "tags: []\ntools: [haddock3]\nobservation_count: 1\ntype: success_pattern\n"
        "last_verified: '2025-01-01'\n---\n\nBody\n"
    )

    pm.decrement_confidence_for_tool(str(project_dir), "haddock3", delta=0.1)

    new_meta = yaml.safe_load(entry.read_text().split("---")[1])
    assert new_meta["confidence"] < 0.9
