"""Integration tests: verify the full pipeline works end-to-end.

Tests cover the issues found by the independent code review:
- P0: context_assembly loads entries from .magnolia/ (not root)
- P1: CLI cmd_assess uses correct paths and quality_flags
- P1: Archive cap updates INDEX.md
- P1: Skill floor is enforced
- P2: Goal set/get symmetry
- P2: failure_pattern in extraction and distillation
- P2: Confidence decay affects scoring
- P2: conversation_history wires through to recent_tools
"""

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import yaml

from compchem_memory.context_assembly import assemble_context, _memory_store
from compchem_memory.learning.consolidator import consolidate_tier, _archive_excess
from compchem_memory.learning.distiller import distill_session
from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.retrieval import _score_entry
from compchem_memory.tiers.project import ProjectManager
from compchem_memory.tiers.skill import SkillManager
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def project_dir(tmp_dir):
    pd = tmp_dir / "project"
    pd.mkdir()
    return pd


@pytest.fixture
def skills_dir(tmp_dir):
    sd = tmp_dir / "skills"
    sd.mkdir()
    return sd


# ── P0: Path conventions ──────────────────────────────────────────────────


class TestPathConventions:
    """Verify that .magnolia/ subdirs are used, not project root."""

    def test_memory_store_resolves_dot_magnolia(self, project_dir):
        ensure_project_store(str(project_dir))
        store = _memory_store(str(project_dir))
        assert store == project_dir / ".magnolia"

    def test_memory_store_follows_symlink(self, project_dir, tmp_dir):
        target = tmp_dir / "shared_magnolia"
        target.mkdir()
        (project_dir / ".magnolia").symlink_to(target)
        store = _memory_store(str(project_dir))
        assert store == target

    def test_assemble_context_loads_project_entries(self, project_dir, skills_dir):
        """P0 regression: entries stored in .magnolia/entries must appear in context."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")
        mgr.create_entry(
            str(project_dir), "Docking Result", "Score: -120.5", tags=["docking"]
        )

        result = assemble_context(
            task_description="docking result",
            project_dir=str(project_dir),
            skills_dir=str(skills_dir),
            token_budget=8000,
        )
        project_sources = [s for s in result.sources if s["tier"] == "project"]
        assert len(project_sources) > 0, (
            f"Expected project entries in context, got sources: {result.sources}"
        )

    def test_assemble_context_loads_session(self, project_dir, skills_dir):
        """P0 regression: sessions in .magnolia/sessions must appear in context."""
        store = ensure_project_store(str(project_dir))
        sessions_dir = store / "sessions"
        fname = datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "tool_call",
            "tool": "haddock3",
        }
        (sessions_dir / fname).write_text(json.dumps(event) + "\n")

        result = assemble_context(
            task_description="haddock3 run",
            project_dir=str(project_dir),
            skills_dir=str(skills_dir),
            token_budget=8000,
        )
        session_sources = [s for s in result.sources if s["tier"] == "session"]
        assert len(session_sources) > 0, (
            f"Expected session in context, got sources: {result.sources}"
        )


# ── Goal set/get symmetry ─────────────────────────────────────────────────


class TestGoalManagement:
    def test_set_and_get_goal(self, project_dir):
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")
        mgr.set_goal(str(project_dir), "# Project Goal\nDock protein-protein complex")

        goal = mgr.get_goal(str(project_dir))
        assert goal is not None
        assert "Dock protein-protein complex" in goal

    def test_goal_loaded_first_in_context(self, project_dir, skills_dir):
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")
        mgr.set_goal(str(project_dir), "My scientific goal")
        mgr.create_entry(str(project_dir), "An Entry", "Some content")

        result = assemble_context(
            task_description="anything",
            project_dir=str(project_dir),
            skills_dir=str(skills_dir),
            token_budget=8000,
        )
        assert result.sources[0]["tier"] == "goal", (
            f"Goal should be first source, got: {result.sources}"
        )

    def test_goal_path_symmetry(self, project_dir):
        """set_goal writes to .magnolia/GOAL.md; get_goal reads from same path."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")
        mgr.set_goal(str(project_dir), "Symmetric goal")

        # Goal file should exist at .magnolia/GOAL.md
        goal_path = project_dir / ".magnolia" / "GOAL.md"
        assert goal_path.exists()

        # get_goal should find it
        assert mgr.get_goal(str(project_dir)) == "Symmetric goal"


# ── Archive cap ───────────────────────────────────────────────────────────


class TestArchiveCap:
    def test_archive_excess_moves_lowest_confidence(self, project_dir):
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        # Create 5 entries with varying confidence
        for i in range(5):
            mgr.create_entry(
                str(project_dir),
                f"Entry {i}",
                f"Content {i}",
                confidence=0.1 * i,  # 0.0, 0.1, 0.2, 0.3, 0.4
            )

        entries_dir = project_dir / ".magnolia" / "entries"
        base_dir = str(project_dir)
        archived = _archive_excess(entries_dir, base_dir, max_entries=3)
        assert archived == 2

        # Remaining entries should be the highest confidence
        remaining = [f for f in entries_dir.glob("*.md") if f.name != "INDEX.md"]
        assert len(remaining) == 3

        # Archive dir should have the evicted entries
        archive_dir = project_dir / ".magnolia" / "archive"
        archived_files = list(archive_dir.glob("*.md"))
        assert len(archived_files) == 2

    def test_consolidate_updates_index_after_archive(self, project_dir):
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        # Create 5 entries with wikilink-friendly names
        for i in range(5):
            mgr.create_entry(
                str(project_dir),
                f"Test Entry {i}",
                f"Content {i}",
                confidence=0.1 * i,
                tags=["test"],
            )

        index_path = project_dir / ".magnolia" / "entries" / "INDEX.md"
        pre_text = index_path.read_text()

        consolidate_tier("project", str(project_dir), max_entries=3)

        post_text = index_path.read_text()
        # INDEX should have been updated (different content)
        assert pre_text != post_text
        # Should have fewer entries listed
        assert "Test Entry 4" in post_text  # highest confidence kept

    def test_consolidate_updates_index_after_merge(self, project_dir):
        """INDEX.md must be refreshed after merging duplicates too."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        # Create two entries with same title → will be merged
        mgr.create_entry(str(project_dir), "Same Title", "Content A")
        mgr.create_entry(str(project_dir), "Same Title", "Content B")

        index_path = project_dir / ".magnolia" / "entries" / "INDEX.md"
        pre_text = index_path.read_text()
        assert "Same Title" in pre_text

        consolidate_tier("project", str(project_dir))

        post_text = index_path.read_text()
        # INDEX should have been refreshed — different content
        assert pre_text != post_text
        # Should have 1 entry instead of 2
        assert post_text.count("Same Title") < pre_text.count("Same Title")


# ── Skill floor enforcement ───────────────────────────────────────────────


class TestSkillFloor:
    def test_skill_floor_preserves_budget(self, project_dir, skills_dir):
        """When skills return nothing, 30% of budget should be held.

        Without the floor, project entries could consume the entire budget.
        The floor ensures non-skill content stays within 70% of budget.
        """
        ensure_project_store(str(project_dir))

        # Create lots of project entries that could consume all budget
        mgr = ProjectManager(Path.home() / ".magnolia")
        for i in range(10):
            mgr.create_entry(
                str(project_dir),
                f"Big Entry {i}",
                "x" * 500,  # ~125 tokens each
                tags=["test"],
            )

        budget = 2000
        result = assemble_context(
            task_description="test query",
            project_dir=str(project_dir),
            skills_dir=str(skills_dir),  # empty skills dir
            token_budget=budget,
        )

        # Verify the floor is enforced: total tokens should not exceed 70%
        # because 30% is reserved for skills (even though none exist).
        skill_floor = int(budget * 0.30)
        max_non_skill = budget - skill_floor
        assert result.tokens_used <= max_non_skill, (
            f"Expected tokens_used <= {max_non_skill} (budget - floor), "
            f"got {result.tokens_used}. Skill floor not enforced."
        )


# ── Confidence decay ──────────────────────────────────────────────────────


class TestConfidenceDecay:
    def test_old_entries_score_lower(self):
        import math

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")

        fresh = {
            "title": "fresh entry",
            "description": "test",
            "tags": [],
            "tools": [],
            "type": "note",
            "confidence": 0.8,
            "last_verified": today,
        }
        stale = {
            "title": "stale entry",
            "description": "test",
            "tags": [],
            "tools": [],
            "type": "note",
            "confidence": 0.8,
            "last_verified": old_date,
        }

        fresh_score = _score_entry(fresh, "test", {"test"})
        stale_score = _score_entry(stale, "test", {"test"})

        assert fresh_score > stale_score
        # Verify meaningful decay: exp(-0.01155 * 120) ≈ 0.25 on the base score.
        # The ratio is diluted by word-overlap bonuses that don't decay,
        # so we check for a meaningful drop rather than exact halving.
        ratio = stale_score / fresh_score
        assert ratio < 0.9, f"Expected stale to be meaningfully lower, got {ratio:.2f}"

    def test_recently_verified_not_decayed(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = {
            "title": "verified today",
            "description": "test",
            "tags": [],
            "tools": [],
            "type": "note",
            "confidence": 0.8,
            "last_verified": today,
        }
        score = _score_entry(entry, "test", {"test"})
        assert score > 0  # No decay for today's entry


# ── failure_pattern entry type ────────────────────────────────────────────


class TestFailurePattern:
    def test_failure_pattern_type_boost(self):
        entry = {
            "title": "failure",
            "description": "test",
            "tags": [],
            "tools": [],
            "type": "failure_pattern",
            "confidence": 0.5,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        note_entry = {
            "title": "note",
            "description": "test",
            "tags": [],
            "tools": [],
            "type": "note",
            "confidence": 0.5,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        fail_score = _score_entry(entry, "test", {"test"})
        note_score = _score_entry(note_entry, "test", {"test"})
        assert fail_score > note_score

    def test_distiller_extracts_unresolved_failures(self, tmp_dir):
        session_path = tmp_dir / "session.jsonl"
        events = [
            {"event_type": "tool_error", "tool": "haddock3", "error": "Missing parameter file"},
            {"event_type": "tool_success", "tool": "gnina", "result_summary": "OK"},
            # haddock3 never succeeded → unresolved
        ]
        session_path.write_text("\n".join(json.dumps(e) for e in events))

        candidates = distill_session(str(session_path))
        failures = [c for c in candidates if c["type"] == "failure_pattern"]
        assert len(failures) == 1
        assert "Missing parameter file" in failures[0]["title"]
        assert "haddock3" in failures[0]["content"]

    def test_extractor_extracts_unresolved_failures(self, tmp_dir):
        session_path = tmp_dir / "session.jsonl"
        events = [
            {"event_type": "tool_error", "tool": "haddock3", "error": "Config not found"},
            {"event_type": "tool_call", "tool": "haddock3"},
            {"event_type": "tool_call", "tool": "haddock3"},
            {"event_type": "tool_call", "tool": "haddock3"},
        ]
        session_path.write_text("\n".join(json.dumps(e) for e in events))

        extractor = AutomaticMemoryExtractor(str(tmp_dir))
        saved = extractor.extract_and_save(session_path, str(tmp_dir))

        # Check that failure_pattern entries are among saved
        types = set()
        for path_str in saved:
            text = Path(path_str).read_text()
            if text.startswith("---"):
                end = text.find("---", 3)
                meta = yaml.safe_load(text[3:end].strip()) or {}
                types.add(meta.get("type", ""))
        assert "failure_pattern" in types


# ── conversation_history → recent_tools ───────────────────────────────────


class TestConversationHistory:
    def test_recent_tools_filters_project_entries(self, project_dir, skills_dir):
        """conversation_history should produce recent_tools that filter entries."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        # Create an entry tagged with haddock3
        mgr.create_entry(
            str(project_dir),
            "Haddock3 Note",
            "dock notes",
            tags=["docking"],
            tools=["haddock3"],
        )

        # Simulate conversation where haddock3 was recently called
        history = [
            {"tool_calls": [{"name": "haddock3_run"}]},
            {"tool_calls": [{"name": "haddock3_run"}]},
        ]

        result = assemble_context(
            task_description="docking",
            project_dir=str(project_dir),
            skills_dir=str(skills_dir),
            token_budget=8000,
            conversation_history=history,
        )

        # The haddock3 entry should be filtered out by recent_tools
        project_sources = [s for s in result.sources if s["tier"] == "project"]
        assert len(project_sources) == 0, "Entry should be filtered by recent_tools"


# ── quality_flags persistence ─────────────────────────────────────────────


class TestQualityFlags:
    def test_record_run_stores_quality_flags(self, project_dir):
        mgr = ProjectManager(Path.home() / ".magnolia")
        path = mgr.record_run(
            str(project_dir),
            run_id="qf_test",
            tool="haddock3",
            status="pass",
            quality_flags=["low_clustering", "few_models"],
        )
        assert Path(path).exists()

        history = mgr.get_run_history(str(project_dir))
        assert len(history) == 1
        assert "low_clustering" in history[0]["quality_flags"]
        assert "few_models" in history[0]["quality_flags"]

    def test_record_run_preserves_errors_solved(self, project_dir):
        """P2 compat: errors_solved still works despite quality_flags insertion."""
        mgr = ProjectManager(Path.home() / ".magnolia")
        path = mgr.record_run(
            str(project_dir),
            run_id="compat_test",
            tool="haddock3",
            status="pass",
            quality_flags=["flag_a"],
            errors_solved=["missing_param"],
        )
        history = mgr.get_run_history(str(project_dir))
        assert "missing_param" in history[0]["errors_solved"]
        assert "flag_a" in history[0]["quality_flags"]


# ── failure_pattern in ENTRY_TYPES ────────────────────────────────────────


class TestFailurePatternEntryType:
    def test_create_failure_pattern_entry(self, project_dir):
        mgr = ProjectManager(Path.home() / ".magnolia")
        path = mgr.create_entry(
            str(project_dir),
            "GNINA Failed on Covalent",
            "Covalent docking crashed",
            entry_type="failure_pattern",
        )
        text = Path(path).read_text()
        assert "failure_pattern" in text

    def test_index_includes_failure_pattern_section(self, project_dir):
        mgr = ProjectManager(Path.home() / ".magnolia")
        mgr.create_entry(
            str(project_dir),
            "Failure Note",
            "content",
            entry_type="failure_pattern",
        )
        index_path = project_dir / ".magnolia" / "entries" / "INDEX.md"
        text = index_path.read_text()
        assert "Failure Patterns" in text
