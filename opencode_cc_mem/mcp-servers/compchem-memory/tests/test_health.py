"""Tests for health check module."""

import pytest
from pathlib import Path
from datetime import datetime, timezone

from compchem_memory.health import run_health_check
from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def tmp_dir():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / ".magnolia" / "entries").mkdir(parents=True)
        (p / ".magnolia" / "staging").mkdir(parents=True)
        yield p


@pytest.fixture
def project_dir(tmp_dir):
    return str(tmp_dir)


@pytest.fixture
def proj_mgr():
    return ProjectManager(Path.home() / ".magnolia")


class TestHealthCheck:
    def test_empty_project_returns_healthy(self, project_dir):
        result = run_health_check(project_dir)
        assert result["status"] == "healthy"
        assert result["issues_found"] == 0
        assert "No issues found" in result["report_markdown"]

    def test_detects_stale_entry(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Stale entry", "old content", tags=["test"]
        )
        # Manually age the entry
        entries_dir = Path(project_dir) / ".magnolia" / "entries"
        for f in entries_dir.glob("*.md"):
            if f.name == "INDEX.md":
                continue
            text = f.read_text()
            old_date = "2020-01-01"
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Frontmatter values are YAML-quoted, e.g. last_verified: '2026-04-15'
            text = text.replace(f"last_verified: '{today}'", f"last_verified: '{old_date}'")
            text = text.replace(f"last_verified: {today}", f"last_verified: {old_date}")
            text = text.replace(f"date: '{today}'", f"date: '{old_date}'")
            text = text.replace(f"date: {today}", f"date: {old_date}")
            f.write_text(text)

        result = run_health_check(project_dir, stale_days=30)
        assert result["issues_found"] >= 1
        stale_issues = [i for i in result["details"] if i["check"] == "stale"]
        assert len(stale_issues) >= 1

    def test_detects_low_confidence(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Low conf", "content", tags=["test"], confidence=0.1
        )
        result = run_health_check(project_dir, min_confidence=0.3)
        low_conf = [i for i in result["details"] if i["check"] == "low_confidence"]
        assert len(low_conf) >= 1

    def test_detects_broken_refs(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Broken ref", "content", tags=["test"],
            related_entries=["nonexistent_id_123"]
        )
        result = run_health_check(project_dir)
        broken = [i for i in result["details"] if i["check"] == "broken_ref"]
        assert len(broken) >= 1
        assert "nonexistent_id_123" in broken[0]["broken_ids"]

    def test_detects_orphaned_entry(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Lonely entry", "content", tags=["unique_tag_xyz"]
        )
        result = run_health_check(project_dir)
        orphaned = [i for i in result["details"] if i["check"] == "orphaned"]
        assert len(orphaned) >= 1

    def test_fix_removes_broken_refs(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Broken ref", "content", tags=["test"],
            related_entries=["nonexistent_id_456"]
        )
        result = run_health_check(project_dir, fix=True)
        assert result["issues_fixed"] >= 1
        # Verify the broken ref was actually removed
        entries_dir = Path(project_dir) / ".magnolia" / "entries"
        for f in entries_dir.glob("*.md"):
            if f.name == "INDEX.md":
                continue
            text = f.read_text()
            assert "nonexistent_id_456" not in text

    def test_dry_run_no_side_effects(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Broken ref", "content", tags=["test"],
            related_entries=["nonexistent_id_789"]
        )
        # Snapshot files before
        entries_dir = Path(project_dir) / ".magnolia" / "entries"
        before = {}
        for f in entries_dir.glob("*.md"):
            if f.name != "INDEX.md":
                before[f.name] = f.read_text()

        run_health_check(project_dir, fix=False)

        # Files unchanged
        for f in entries_dir.glob("*.md"):
            if f.name != "INDEX.md":
                assert f.read_text() == before[f.name]

    def test_report_markdown_has_sections(self, project_dir):
        result = run_health_check(project_dir)
        md = result["report_markdown"]
        assert "# Memory Health Check Report" in md
        assert "## Summary" in md
