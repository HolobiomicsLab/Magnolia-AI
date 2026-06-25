import pytest
from pathlib import Path
import tempfile

from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        pd = Path(d) / "project"
        pd.mkdir()
        yield pd


def _mgr(project_dir):
    # tests construct ProjectManager with the project dir as global_base
    return ProjectManager(project_dir)


def test_returns_failure_entries_for_tool(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "haddock3 OOM on Azzurra", "32 CNS workers exceeded 32GB",
                     entry_type="failure_pattern", tools=["haddock3"])
    mgr.create_entry(pd, "gromacs box too small", "pbc errors",
                     entry_type="error_resolution", tools=["gromacs"])
    mgr.create_entry(pd, "a plain note about haddock3", "not a warning",
                     entry_type="note", tools=["haddock3"])

    hits = mgr.select_warnings_for_tool(pd, "haddock3")

    assert len(hits) == 1
    assert hits[0]["title"] == "haddock3 OOM on Azzurra"
    assert hits[0]["type"] == "failure_pattern"
    assert hits[0]["provisional"] is False
    assert "32GB" in hits[0]["summary"]  # from frontmatter description (content[:200])


def test_tool_match_is_case_insensitive_and_excludes_other_tools(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "w", "x", entry_type="failure_pattern", tools=["HADDOCK3"])
    hits = mgr.select_warnings_for_tool(pd, "haddock3")
    assert len(hits) == 1


def test_staging_hits_flagged_provisional_and_ordered_after_promoted(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "promoted warn", "p", entry_type="failure_pattern", tools=["qm"])
    mgr.create_entry(pd, "staging warn", "s", entry_type="failure_pattern", tools=["qm"],
                     staging=True)
    hits = mgr.select_warnings_for_tool(pd, "qm")
    assert [h["provisional"] for h in hits] == [False, True]  # promoted first


def test_limit_respected(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    for i in range(7):
        mgr.create_entry(pd, f"warn {i}", "c", entry_type="failure_pattern", tools=["p2rank"])
    assert len(mgr.select_warnings_for_tool(pd, "p2rank", limit=3)) == 3


def test_empty_when_no_match_or_falsy_tool(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "w", "c", entry_type="failure_pattern", tools=["haddock3"])
    assert mgr.select_warnings_for_tool(pd, "gnina") == []
    assert mgr.select_warnings_for_tool(pd, "") == []


def test_module_wrapper_returns_plain_list(project_dir, monkeypatch):
    # warnings_for_tool resolves store + manager and returns a list (not JSON)
    from compchem_memory import recall
    monkeypatch.setattr(recall, "GLOBAL_BASE", project_dir)
    monkeypatch.setattr(recall, "resolve_project_dir", lambda pd, default=".": str(project_dir))
    monkeypatch.setattr(recall, "ensure_project_store", lambda pd: None)
    ProjectManager(project_dir).create_entry(
        str(project_dir), "w", "c", entry_type="failure_pattern", tools=["haddock3"])
    out = recall.warnings_for_tool("haddock3", str(project_dir))
    assert isinstance(out, list) and len(out) == 1
    assert recall.warnings_for_tool("") == []
