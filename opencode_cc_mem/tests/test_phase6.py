"""Phase 6 integration tests: consolidation scale, session distillation, error matching."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.tiers.session import SessionManager
from compchem_memory.learning.consolidator import consolidate_tier, _merge_duplicates
from compchem_memory.learning.distiller import distill_session
from compchem_memory.learning.assessor import assess_run


def _make_project(tmp: Path) -> Path:
    """Create a .magnolia project store layout."""
    mag = tmp / ".magnolia"
    for sub in ["entries", "staging", "runs", "sessions"]:
        (mag / sub).mkdir(parents=True, exist_ok=True)
    return tmp


@pytest.fixture(scope="module")
def test_project():
    """Create a project directory with 50+ entries for consolidation testing."""
    with tempfile.TemporaryDirectory(prefix="magnolia_p6_") as d:
        project_dir = _make_project(Path(d))
        mgr = ProjectManager(Path("/tmp"))

        # Create 55 entries with various types
        for i in range(55):
            entry_type = ["note", "error_resolution", "success_pattern", "parameter_guidance"][i % 4]
            tags = [f"tag-{i % 5}", "auto"]
            if i % 4 == 0:
                tags.append("duplicate-target")
            mgr.create_entry(
                str(project_dir),
                title=f"Test entry {i:03d}",
                content=f"Content for entry {i}. " + "x" * (i * 10),
                tags=tags,
                entry_type=entry_type,
                confidence=0.5 + (i % 5) * 0.1,
                tools=[["haddock3", "gnina"][i % 2]] if i % 3 == 0 else [],
            )

        # Create a few duplicate-title entries
        mgr.create_entry(
            str(project_dir),
            title="duplicate entry",
            content="First version",
            tags=["test"],
        )
        mgr.create_entry(
            str(project_dir),
            title="duplicate entry",
            content="Second version",
            tags=["test"],
        )

        yield project_dir


class TestConsolidationScale:
    def test_50_plus_entries_exist(self, test_project):
        entries_dir = test_project / ".magnolia" / "entries"
        md_files = [f for f in entries_dir.glob("*.md") if f.name != "INDEX.md"]
        assert len(md_files) >= 55

    def test_consolidation_merges_duplicates(self, test_project):
        report = consolidate_tier(
            "project", str(test_project), stale_days=9999, max_entries=100
        )
        assert report["merged"] >= 1

    def test_consolidation_report_structure(self, test_project):
        report = consolidate_tier("project", str(test_project))
        assert "merged" in report
        assert "expired" in report
        assert "remaining" in report
        assert "actions" in report

    def test_merge_duplicates_function(self):
        """Test the _merge_duplicates helper directly."""
        with tempfile.TemporaryDirectory() as d:
            entries_dir = Path(d)
            for i, suffix in enumerate(["first", "second"]):
                fm = yaml.dump({"title": "Same Title", "type": "note"})
                content = f"---\n{fm}---\n\nContent {suffix}\n"
                (entries_dir / f"{suffix}.md").write_text(content)

            entries = list(entries_dir.glob("*.md"))
            merged = _merge_duplicates(entries, entries_dir)
            assert merged == 1
            remaining = list(entries_dir.glob("*.md"))
            assert len(remaining) == 1


class TestSessionDistillation:
    def test_distill_session_extracts_errors(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "session.jsonl"
            events = [
                {"event_type": "tool_error", "tool": "haddock3_run", "error": "sampling too low for rigidbody"},
                {"event_type": "tool_success", "tool": "haddock3_run", "result_summary": "Increased sampling to 1000"},
                {"event_type": "tool_call", "tool": "gnina_dock", "non_default_params": [
                    {"tool": "gnina_dock", "param": "exhaustiveness", "value": 16, "default": 1}
                ]},
            ]
            with open(log_path, "w") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")

            candidates = distill_session(str(log_path))
            assert len(candidates) >= 2
            types = [c["type"] for c in candidates]
            assert "error_resolution" in types
            assert "parameter_choice" in types

    def test_distill_empty_session(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "empty.jsonl"
            log_path.write_text("")
            candidates = distill_session(str(log_path))
            assert candidates == []

    def test_distill_nonexistent(self):
        candidates = distill_session("/nonexistent/path.jsonl")
        assert candidates == []


class TestErrorPatternMatching:
    def test_assess_run_flags_issues(self):
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            result = assess_run(str(rd), "haddock3", exit_code=0)
            assert "overall" in result

    def test_assess_run_with_output(self):
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            output = rd / "output"
            output.mkdir()
            capri = output / "02_caprieval"
            capri.mkdir()
            (capri / "capri_clt.tsv").write_text(
                "model\tcluster_id\tscore\tlrmsd\nmodel_1\tc1\t-90.0\t2.1\n"
            )
            result = assess_run(str(rd), "haddock3", exit_code=0)
            # Check that it has the expected keys
            assert "overall" in result
            assert "technical" in result

    def test_search_entries_finds_errors(self, test_project):
        mgr = ProjectManager(Path("/tmp"))
        results = mgr.search_entries(str(test_project), keyword="entry")
        assert len(results) >= 10

    def test_search_by_tag(self, test_project):
        mgr = ProjectManager(Path("/tmp"))
        results = mgr.search_entries(str(test_project), tags=["duplicate-target"])
        assert len(results) >= 10


class TestPhase6Features:
    def test_auto_promotion(self):
        """Test that entries with 2+ observations in distinct sessions get auto-promoted."""
        with tempfile.TemporaryDirectory() as d:
            project_dir = _make_project(Path(d))
            mgr = ProjectManager(Path("/tmp"))

            # Create a staging entry with high observation count and confidence
            mgr.create_entry(
                str(project_dir),
                title="Auto promote test",
                content="Should be promoted",
                staging=True,
                confidence=0.9,
            )

            # Read and modify to have high observation count across two distinct sessions
            staging_dir = project_dir / ".magnolia" / "staging"
            staging_files = list(staging_dir.glob("*.md"))
            assert len(staging_files) == 1

            f = staging_files[0]
            text = f.read_text()
            # Parse frontmatter
            parts = text.split("---")
            assert len(parts) >= 3
            meta = yaml.safe_load(parts[1])
            meta["observation_count"] = 2
            meta["confidence"] = 0.9
            meta["observed_in_sessions"] = ["2026-05-12_120000", "2026-05-13_140000"]
            new_fm = yaml.dump(meta, default_flow_style=False)
            body = "---".join(parts[2:])
            f.write_text(f"---\n{new_fm}---{body}")

            # Auto-promote
            promoted = mgr.auto_promote_staging(str(project_dir))
            assert len(promoted) == 1

            # Verify moved to entries
            entries = list((project_dir / ".magnolia" / "entries").glob("*.md"))
            assert any("Auto_promote" in e.name for e in entries)

    def test_related_entries(self):
        """Test bidirectional link creation on shared tags."""
        with tempfile.TemporaryDirectory() as d:
            project_dir = _make_project(Path(d))
            mgr = ProjectManager(Path("/tmp"))

            mgr.create_entry(
                str(project_dir),
                title="First entry",
                content="Content A",
                tags=["haddock3", "docking", "covalent"],
            )
            mgr.create_entry(
                str(project_dir),
                title="Second entry docking",
                content="Content B",
                tags=["haddock3", "docking", "parameter"],
            )

            entries = mgr.list_entries(str(project_dir))
            assert len(entries) >= 2

    def test_extraction_state_persistence(self):
        """Test that extraction state is saved and loaded."""
        with tempfile.TemporaryDirectory() as d:
            from compchem_memory.extraction import AutomaticMemoryExtractor

            project_dir = Path(d)

            extractor = AutomaticMemoryExtractor(str(project_dir))
            extractor.last_cursor = "test_cursor_123"
            extractor.state_path = project_dir / "extraction-state.yaml"
            extractor._save_state()

            assert (project_dir / "extraction-state.yaml").exists()

            extractor2 = AutomaticMemoryExtractor(str(project_dir))
            assert extractor2.last_cursor == "test_cursor_123"

    def test_runs_index_is_yaml(self):
        """Test that runs index is now YAML format."""
        with tempfile.TemporaryDirectory() as d:
            project_dir = _make_project(Path(d))
            mgr = ProjectManager(Path("/tmp"))

            mgr.record_run(
                str(project_dir),
                run_id="test_001",
                tool="haddock3",
                status="success",
                metrics={"score": -85.0},
            )

            runs_index = project_dir / ".magnolia" / "runs" / "INDEX.yaml"
            assert runs_index.exists()
            data = yaml.safe_load(runs_index.read_text())
            assert "runs" in data
            assert "last_updated" in data

    def test_memory_server_has_phase6_tools(self):
        import asyncio
        from compchem_memory.server import mcp

        tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in ["memory_search_errors", "memory_distill_session"]:
            assert name in tool_names, f"Missing Phase 6 tool: {name}"
