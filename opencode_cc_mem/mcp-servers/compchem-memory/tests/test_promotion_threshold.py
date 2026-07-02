"""Tests for N=2 cross-session promotion threshold (closes §3.4)."""

import yaml
from pathlib import Path

import pytest

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def _make_staging_entry(pd, name, content, frontmatter):
    p = pd / ".magnolia" / "staging" / f"{name}.md"
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    p.write_text(f"---\n{fm}---\n\n{content}\n")
    return p


def test_two_obs_same_session_does_not_promote(project_dir):
    pm = ProjectManager(Path.home() / ".magnolia")
    _make_staging_entry(
        project_dir,
        "20260512_120000_test_entry",
        "Body.",
        {
            "title": "Test entry",
            "confidence": 0.9,
            "observation_count": 2,
            "observed_in_sessions": ["2026-05-12_120000", "2026-05-12_120000"],
            "tags": ["x"],
            "type": "note",
        },
    )
    promoted = pm.auto_promote_staging(str(project_dir))
    assert promoted == [], "Two observations in same session must not promote"


def test_two_obs_distinct_sessions_promotes(project_dir):
    pm = ProjectManager(Path.home() / ".magnolia")
    _make_staging_entry(
        project_dir,
        "20260512_120000_test_entry",
        "Body.",
        {
            "title": "Test entry",
            "confidence": 0.9,
            "observation_count": 2,
            "observed_in_sessions": ["2026-05-12_120000", "2026-05-13_140000"],
            "tags": ["x"],
            "type": "note",
        },
    )
    promoted = pm.auto_promote_staging(str(project_dir))
    assert len(promoted) == 1


def test_one_obs_does_not_promote_regardless_of_confidence(project_dir):
    pm = ProjectManager(Path.home() / ".magnolia")
    _make_staging_entry(
        project_dir,
        "20260512_120000_test_entry",
        "Body.",
        {
            "title": "Test entry",
            "confidence": 0.99,
            "observation_count": 1,
            "observed_in_sessions": ["2026-05-12_120000"],
            "tags": ["x"],
            "type": "note",
        },
    )
    promoted = pm.auto_promote_staging(str(project_dir))
    assert promoted == []
