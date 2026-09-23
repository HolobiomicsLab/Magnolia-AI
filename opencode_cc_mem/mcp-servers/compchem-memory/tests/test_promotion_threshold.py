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


def test_confidence_exactly_at_threshold_promotes(project_dir):
    """conf == 0.85 satisfies the >= 0.85 bar. Live incident 2026-09-18: an
    obs=7 entry sat in staging forever because the bar was strict '>'."""
    pm = ProjectManager(Path.home() / ".magnolia")
    _make_staging_entry(
        project_dir,
        "20260512_120000_boundary_entry",
        "Body.",
        {
            "title": "Boundary entry",
            "confidence": 0.85,
            "observation_count": 2,
            "observed_in_sessions": ["2026-05-12_120000", "2026-05-13_140000"],
            "tags": ["x"],
            "type": "note",
        },
    )
    promoted = pm.auto_promote_staging(str(project_dir))
    assert len(promoted) == 1, "confidence exactly at 0.85 must promote"


def test_confidence_below_threshold_does_not_promote(project_dir):
    pm = ProjectManager(Path.home() / ".magnolia")
    _make_staging_entry(
        project_dir,
        "20260512_120000_below_entry",
        "Body.",
        {
            "title": "Below threshold entry",
            "confidence": 0.84,
            "observation_count": 2,
            "observed_in_sessions": ["2026-05-12_120000", "2026-05-13_140000"],
            "tags": ["x"],
            "type": "note",
        },
    )
    promoted = pm.auto_promote_staging(str(project_dir))
    assert promoted == []
