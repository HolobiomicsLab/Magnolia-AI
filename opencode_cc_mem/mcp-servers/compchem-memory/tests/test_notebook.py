"""Tests for notebook module."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from compchem_memory.notebook import generate_notebook
from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def tmp_dir():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        for sub in ["entries", "runs", "sessions", "session-notes"]:
            (p / ".magnolia" / sub).mkdir(parents=True)
        yield p


@pytest.fixture
def project_dir(tmp_dir):
    return str(tmp_dir)


@pytest.fixture
def proj_mgr():
    return ProjectManager(Path.home() / ".magnolia")


class TestNotebook:
    def test_empty_project_returns_header(self, project_dir):
        result = generate_notebook(project_dir)
        assert "# Lab Notebook:" in result
        assert "No records found" in result

    def test_includes_entries(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Test Entry", "Some content", tags=["test"]
        )
        result = generate_notebook(project_dir)
        assert "Test Entry" in result
        assert "Some content" in result

    def test_includes_runs(self, project_dir, proj_mgr):
        proj_mgr.record_run(
            project_dir, "run_001", "haddock3", "success",
            metrics={"score": -120.5}
        )
        result = generate_notebook(project_dir)
        assert "run_001" in result
        assert "haddock3" in result

    def test_date_filtering(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Today Entry", "content", tags=["test"]
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = generate_notebook(project_dir, start_date=today, end_date=today)
        assert "Today Entry" in result

        result_past = generate_notebook(project_dir, start_date="2020-01-01", end_date="2020-12-31")
        assert "Today Entry" not in result_past

    def test_section_filter(self, project_dir, proj_mgr):
        proj_mgr.create_entry(
            project_dir, "Test Entry", "content", tags=["test"]
        )
        proj_mgr.record_run(
            project_dir, "run_001", "haddock3", "success"
        )
        # Only runs
        result = generate_notebook(project_dir, section="runs")
        assert "run_001" in result
        assert "Test Entry" not in result

        # Only entries
        result_entries = generate_notebook(project_dir, section="entries")
        assert "Test Entry" in result_entries
        assert "run_001" not in result_entries

    def test_includes_session_activity(self, project_dir):
        sessions_dir = Path(project_dir) / ".magnolia" / "sessions"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_file = sessions_dir / f"{today}_120000.jsonl"
        events = [
            {"timestamp": "2026-04-15T12:00:00", "event_type": "tool_call", "tool": "haddock3_run"},
            {"timestamp": "2026-04-15T12:01:00", "event_type": "tool_success", "tool": "haddock3_run"},
            {"timestamp": "2026-04-15T12:02:00", "event_type": "observation"},
        ]
        with open(session_file, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        result = generate_notebook(project_dir)
        assert "3 events recorded" in result
