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

        candidates = AutomaticMemoryExtractor(str(tmp_dir)).preview(session_path)
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
        saved = extractor.commit(session_path, str(tmp_dir))

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


# ── Run outcome feedback into retrieval scoring (2.1) ────────────────────


class TestRunOutcomeScoring:
    def test_failed_runs_boost_error_resolution(self, project_dir):
        """Entries about error_resolution for a failed tool should score higher."""
        ensure_project_store(str(project_dir))
        store = project_dir / ".magnolia"

        # Write a failed run YAML
        runs_dir = store / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        (runs_dir / "20260422_fail1.yaml").write_text(
            _yaml.dump({"run_id": "fail1", "tool": "haddock3", "status": "fail", "date": "2026-04-22", "metrics": {}, "quality_flags": [], "errors_solved": []})
        )

        error_entry = {
            "title": "haddock3 missing param",
            "description": "fix for haddock3",
            "tags": [],
            "tools": ["haddock3"],
            "type": "error_resolution",
            "confidence": 0.5,
            "last_verified": "2026-04-22",
        }
        from compchem_memory.retrieval import _score_entry, _load_recent_run_outcomes

        outcomes = _load_recent_run_outcomes(str(store))
        assert outcomes.get("haddock3") == "fail"

        score_with_outcome = _score_entry(error_entry, "haddock3", {"haddock3"}, run_outcomes=outcomes)
        score_without = _score_entry(error_entry, "haddock3", {"haddock3"}, run_outcomes={})
        assert score_with_outcome > score_without * 1.5, (
            f"Error resolution should be boosted after failed run: {score_with_outcome} vs {score_without}"
        )

    def test_failed_runs_penalize_success_pattern(self, project_dir):
        """Success pattern entries for a failed tool should score lower."""
        ensure_project_store(str(project_dir))
        store = project_dir / ".magnolia"
        runs_dir = store / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        (runs_dir / "20260422_fail1.yaml").write_text(
            _yaml.dump({"run_id": "fail1", "tool": "haddock3", "status": "fail", "date": "2026-04-22", "metrics": {}, "quality_flags": [], "errors_solved": []})
        )

        success_entry = {
            "title": "haddock3 works great",
            "description": "haddock3 success",
            "tags": [],
            "tools": ["haddock3"],
            "type": "success_pattern",
            "confidence": 0.5,
            "last_verified": "2026-04-22",
        }
        from compchem_memory.retrieval import _score_entry, _load_recent_run_outcomes

        outcomes = _load_recent_run_outcomes(str(store))
        score_with = _score_entry(success_entry, "haddock3", {"haddock3"}, run_outcomes=outcomes)
        score_without = _score_entry(success_entry, "haddock3", {"haddock3"}, run_outcomes={})
        assert score_with < score_without, (
            f"Success pattern should be penalized after failed run: {score_with} vs {score_without}"
        )

    def test_passed_runs_no_effect(self):
        """When all runs pass, no boost or penalty is applied."""
        from compchem_memory.retrieval import _score_entry
        entry = {
            "title": "haddock3 note",
            "description": "test",
            "tags": [],
            "tools": ["haddock3"],
            "type": "note",
            "confidence": 0.5,
            "last_verified": "2026-04-22",
        }
        score_pass = _score_entry(entry, "test", {"test"}, run_outcomes={"haddock3": "pass"})
        score_none = _score_entry(entry, "test", {"test"}, run_outcomes=None)
        assert score_pass == score_none

    def test_warning_runs_boost_error_resolution_softer(self):
        """Warning-level outcomes should apply softer multipliers than failures."""
        from compchem_memory.retrieval import _score_entry
        error_entry = {
            "title": "haddock3 error fix",
            "description": "test",
            "tags": [],
            "tools": ["haddock3"],
            "type": "error_resolution",
            "confidence": 0.5,
            "last_verified": "2026-04-22",
        }
        score_warn = _score_entry(error_entry, "test", {"test"}, run_outcomes={"haddock3": "warning"})
        score_fail = _score_entry(error_entry, "test", {"test"}, run_outcomes={"haddock3": "fail"})
        score_none = _score_entry(error_entry, "test", {"test"}, run_outcomes={})
        # Warning boost (1.5x) should be less than fail boost (2.0x), both > no outcome
        assert score_none < score_warn < score_fail

    def test_old_run_failures_ignored(self, project_dir):
        """Run failures older than 30 days should not affect scoring."""
        ensure_project_store(str(project_dir))
        store = project_dir / ".magnolia"
        runs_dir = store / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        # Write a failed run from 60 days ago
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        (runs_dir / f"{old_date.replace('-', '')}_old_fail.yaml").write_text(
            _yaml.dump({"run_id": "old_fail", "tool": "haddock3", "status": "fail", "date": old_date, "metrics": {}, "quality_flags": [], "errors_solved": []})
        )

        from compchem_memory.retrieval import _load_recent_run_outcomes
        outcomes = _load_recent_run_outcomes(str(store))
        # Old failure should be excluded by the 30-day window
        assert "haddock3" not in outcomes or outcomes.get("haddock3") != "fail"

    def test_select_relevant_uses_run_outcomes(self, project_dir, skills_dir):
        """select_relevant_entries should load and use run outcomes."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        # Create error_resolution entry for haddock3
        mgr.create_entry(
            str(project_dir),
            "Haddock3 Error Fix",
            "Fix for missing parameter",
            tags=["haddock3"],
            tools=["haddock3"],
            entry_type="error_resolution",
        )
        # Create a note entry (unrelated)
        mgr.create_entry(
            str(project_dir),
            "Random Note",
            "Unrelated content",
        )

        # Write a failed run
        store = project_dir / ".magnolia"
        runs_dir = store / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        (runs_dir / "20260422_fail1.yaml").write_text(
            _yaml.dump({"run_id": "fail1", "tool": "haddock3", "status": "fail", "date": "2026-04-22", "metrics": {}, "quality_flags": [], "errors_solved": []})
        )

        from compchem_memory.retrieval import select_relevant_entries
        entries = select_relevant_entries(
            "haddock3 docking",
            str(store),
            budget=8000,
            max_selections=5,
        )
        # The error_resolution entry should be present and have high relevance
        haddock_entries = [e for e in entries if "haddock3" in e.get("title", "").lower()]
        assert len(haddock_entries) > 0, "Error resolution entry should be selected"


# ── Negative confidence feedback (2.4) ───────────────────────────────────


class TestNegativeConfidenceFeedback:
    def _age_entry(self, mgr, path, days_ago: int):
        """Set last_verified to N days ago using the production write path."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        mgr._update_entry_frontmatter(Path(path), "last_verified", old_date)

    def _read_confidence(self, path) -> float:
        """Parse confidence from an entry file's frontmatter."""
        text = Path(path).read_text()
        end = text.find("---", 3)
        meta = yaml.safe_load(text[3:end].strip()) or {}
        return meta.get("confidence", -1)

    def test_decrement_on_failed_tool(self, project_dir):
        """Success pattern entries for a failed tool should lose confidence."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        path = mgr.create_entry(
            str(project_dir),
            "Haddock3 Works Well",
            "Great results",
            tools=["haddock3"],
            entry_type="success_pattern",
            confidence=0.9,
        )
        self._age_entry(mgr, path, 30)

        adjusted = mgr.decrement_confidence_for_tool(str(project_dir), "haddock3", delta=0.1)
        assert adjusted == 1
        assert self._read_confidence(path) == 0.8

    def test_no_decrement_for_recently_verified(self, project_dir):
        """Recently verified entries should NOT be decremented."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        path = mgr.create_entry(
            str(project_dir),
            "Recent Success",
            "Recently verified",
            tools=["haddock3"],
            entry_type="success_pattern",
            confidence=0.9,
        )
        # last_verified is today — should NOT be decremented
        adjusted = mgr.decrement_confidence_for_tool(str(project_dir), "haddock3")
        assert adjusted == 0

    def test_no_decrement_for_unrelated_tool(self, project_dir):
        """Entries for other tools should not be affected."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        path = mgr.create_entry(
            str(project_dir),
            "GNINA Works",
            "Great results",
            tools=["gnina"],
            entry_type="success_pattern",
            confidence=0.9,
        )
        self._age_entry(mgr, path, 30)

        adjusted = mgr.decrement_confidence_for_tool(str(project_dir), "haddock3")
        assert adjusted == 0

    def test_no_decrement_for_non_success_entries(self, project_dir):
        """Only success_pattern entries should be decremented."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        path = mgr.create_entry(
            str(project_dir),
            "Haddock3 Error",
            "Error notes",
            tools=["haddock3"],
            entry_type="error_resolution",
            confidence=0.9,
        )
        self._age_entry(mgr, path, 30)

        adjusted = mgr.decrement_confidence_for_tool(str(project_dir), "haddock3")
        assert adjusted == 0

    def test_confidence_floor_at_zero(self, project_dir):
        """Confidence should not go below 0."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        path = mgr.create_entry(
            str(project_dir),
            "Low Conf Success",
            "Barely works",
            tools=["haddock3"],
            entry_type="success_pattern",
            confidence=0.02,
        )
        self._age_entry(mgr, path, 30)

        mgr.decrement_confidence_for_tool(str(project_dir), "haddock3", delta=0.1)
        assert self._read_confidence(path) == 0.0

    def test_e2e_post_run_assess_decrements_confidence(self, project_dir, skills_dir):
        """End-to-end: assess_run + record_run + decrement on failure
        should decrement confidence on success_pattern entries."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        # Create a success_pattern entry for haddock3 with old date
        path = mgr.create_entry(
            str(project_dir),
            "Haddock3 Great Results",
            "Always works",
            tools=["haddock3"],
            entry_type="success_pattern",
            confidence=0.9,
        )
        self._age_entry(mgr, path, 30)
        assert self._read_confidence(path) == 0.9

        # Simulate what post_run_assess does: assess, record, decrement on fail
        from compchem_memory.learning.assessor import assess_run
        run_dir = project_dir / "runs" / "haddock3_test"
        run_dir.mkdir(parents=True, exist_ok=True)

        assessment = assess_run(str(run_dir), "haddock3", exit_code=1)
        overall = assessment.get("overall", "failed")
        mgr.record_run(
            str(project_dir),
            run_id="test_fail",
            tool="haddock3",
            status=overall,
            metrics=assessment.get("metrics", {}),
            quality_flags=assessment.get("quality_flags", []),
        )

        # The decrement that post_run_assess would trigger
        if overall in ("fail", "failed"):
            mgr.decrement_confidence_for_tool(str(project_dir), "haddock3")

        # Verify confidence was decremented
        assert self._read_confidence(path) < 0.9


# ── Tool name case normalization ─────────────────────────────────────────


class TestToolNameCaseNormalization:
    def test_mixed_case_tool_in_run_matches_entry(self, project_dir):
        """Run YAML with tool: Haddock3 should match entry tools: [haddock3]."""
        ensure_project_store(str(project_dir))
        store = project_dir / ".magnolia"
        runs_dir = store / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        # Write run with mixed-case tool name
        (runs_dir / "20260423_upper.yaml").write_text(
            _yaml.dump({"run_id": "upper", "tool": "Haddock3", "status": "fail", "date": "2026-04-23", "metrics": {}, "quality_flags": [], "errors_solved": []})
        )

        entry = {
            "title": "haddock3 error",
            "description": "test",
            "tags": [],
            "tools": ["haddock3"],  # lowercase
            "type": "error_resolution",
            "confidence": 0.5,
            "last_verified": "2026-04-23",
        }
        from compchem_memory.retrieval import _score_entry, _load_recent_run_outcomes
        outcomes = _load_recent_run_outcomes(str(store))
        assert outcomes.get("haddock3") == "fail", f"Expected haddock3→fail, got {outcomes}"

        score_with = _score_entry(entry, "test", {"test"}, run_outcomes=outcomes)
        score_without = _score_entry(entry, "test", {"test"}, run_outcomes={})
        assert score_with > score_without, "Mixed-case tool should still trigger boost"


# ── Warning penalizes success_pattern ────────────────────────────────────


class TestWarningPenalty:
    def test_warning_penalizes_success_pattern(self):
        from compchem_memory.retrieval import _score_entry
        entry = {
            "title": "haddock3 success",
            "description": "test",
            "tags": [],
            "tools": ["haddock3"],
            "type": "success_pattern",
            "confidence": 0.5,
            "last_verified": "2026-04-23",
        }
        score_warn = _score_entry(entry, "test", {"test"}, run_outcomes={"haddock3": "warning"})
        score_none = _score_entry(entry, "test", {"test"}, run_outcomes={})
        assert score_warn < score_none, "Warning should penalize success_pattern"


# ── failure_pattern + run outcome interaction ────────────────────────────


class TestFailurePatternRunOutcome:
    def test_failure_pattern_gets_double_boost(self):
        """failure_pattern entries get type_boost=2.5 AND run_outcome boost=2.0."""
        from compchem_memory.retrieval import _score_entry
        entry = {
            "title": "haddock3 failed",
            "description": "test",
            "tags": [],
            "tools": ["haddock3"],
            "type": "failure_pattern",
            "confidence": 0.5,
            "last_verified": "2026-04-23",
        }
        score_fail = _score_entry(entry, "test", {"test"}, run_outcomes={"haddock3": "fail"})
        score_pass = _score_entry(entry, "test", {"test"}, run_outcomes={"haddock3": "pass"})
        assert score_fail > score_pass * 1.5, (
            f"failure_pattern should get multiplicative boost from failed run: {score_fail} vs {score_pass}"
        )


# ── max_runs limit ───────────────────────────────────────────────────────


class TestMaxRunsLimit:
    def test_max_runs_limits_to_20(self, project_dir):
        """Only the 20 most recent runs should be considered. Create 25 passes
        with recent dates and 5 fails with older-but-still-within-30-day dates.
        The 5 fails should be excluded because max_runs=20 caps at the newest 20."""
        ensure_project_store(str(project_dir))
        store = project_dir / ".magnolia"
        runs_dir = store / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml

        today = datetime.now(timezone.utc)
        # 5 fails from 10 days ago
        for i in range(5):
            d = (today - timedelta(days=10)).strftime("%Y%m%d")
            (runs_dir / f"{d}_fail{i}.yaml").write_text(
                _yaml.dump({"run_id": f"fail{i}", "tool": "haddock3", "status": "fail",
                            "date": (today - timedelta(days=10)).strftime("%Y-%m-%d"),
                            "metrics": {}, "quality_flags": [], "errors_solved": []})
            )
        # 20 passes from today (newer filenames, will be sorted first)
        for i in range(20):
            (runs_dir / f"{today.strftime('%Y%m%d')}_pass{i}.yaml").write_text(
                _yaml.dump({"run_id": f"pass{i}", "tool": "haddock3", "status": "pass",
                            "date": today.strftime("%Y-%m-%d"),
                            "metrics": {}, "quality_flags": [], "errors_solved": []})
            )

        from compchem_memory.retrieval import _load_recent_run_outcomes
        outcomes = _load_recent_run_outcomes(str(store), max_runs=20)
        # The 20 most recent (today's passes) should be within max_runs;
        # the 10-day-old fails are still within 30-day window but sorted after
        # today's 20 passes, so they're cut off by max_runs=20.
        assert outcomes.get("haddock3") == "pass", (
            f"Expected pass (fails cut off by max_runs), got {outcomes}"
        )

    def test_recent_failure_within_max_runs(self, project_dir):
        """A recent failure within max_runs should be detected."""
        ensure_project_store(str(project_dir))
        store = project_dir / ".magnolia"
        runs_dir = store / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml

        # 19 passes, 1 fail (most recent)
        for i in range(19):
            day = f"{23 - i:02d}"
            (runs_dir / f"202604{day}_pass{i}.yaml").write_text(
                _yaml.dump({"run_id": f"pass{i}", "tool": "haddock3", "status": "pass", "date": f"2026-04-{day}", "metrics": {}, "quality_flags": [], "errors_solved": []})
            )
        (runs_dir / "20260423_fail.yaml").write_text(
            _yaml.dump({"run_id": "fail", "tool": "haddock3", "status": "fail", "date": "2026-04-23", "metrics": {}, "quality_flags": [], "errors_solved": []})
        )

        from compchem_memory.retrieval import _load_recent_run_outcomes
        outcomes = _load_recent_run_outcomes(str(store))
        assert outcomes.get("haddock3") == "fail"


# ── Multiple entries decremented ─────────────────────────────────────────


class TestMultipleDecrement:
    def test_multiple_success_entries_decremented(self, project_dir):
        """All success_pattern entries for a failed tool should be decremented."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        original_confs = []
        paths = []
        for i in range(3):
            conf = 0.8 + i * 0.05
            p = mgr.create_entry(
                str(project_dir),
                f"Haddock3 Success {i}",
                f"Works great {i}",
                tools=["haddock3"],
                entry_type="success_pattern",
                confidence=conf,
            )
            original_confs.append(conf)
            old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
            mgr._update_entry_frontmatter(Path(p), "last_verified", old_date)
            paths.append(p)

        adjusted = mgr.decrement_confidence_for_tool(str(project_dir), "haddock3", delta=0.1)
        assert adjusted == 3

        for p, orig in zip(paths, original_confs):
            text = Path(p).read_text()
            end = text.find("---", 3)
            meta = yaml.safe_load(text[3:end].strip()) or {}
            assert meta["confidence"] < orig, (
                f"Expected confidence < {orig}, got {meta['confidence']}"
            )


# ── Consolidator merge frontmatter ───────────────────────────────────────


class TestConsolidatorMerge:
    def test_merge_combines_frontmatter_not_raw_concat(self, project_dir):
        """Merged entries should have combined frontmatter, not two YAML blocks."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        # Create two entries with same title but different tags/tools/confidence
        mgr.create_entry(
            str(project_dir), "Same Title", "Content A",
            tags=["tag_a"], tools=["tool_a"], confidence=0.3,
        )
        mgr.create_entry(
            str(project_dir), "Same Title", "Content B",
            tags=["tag_b"], tools=["tool_b"], confidence=0.9,
        )

        from compchem_memory.learning.consolidator import consolidate_tier
        consolidate_tier("project", str(project_dir))

        entries_dir = project_dir / ".magnolia" / "entries"
        remaining = [f for f in entries_dir.glob("*.md") if f.name != "INDEX.md"]
        assert len(remaining) == 1, f"Expected 1 merged entry, got {len(remaining)}"

        text = remaining[0].read_text()
        # Should have only ONE frontmatter block at the top
        # (body separator "---" is fine, but frontmatter must be a single block)
        # Verify the second "---" closes frontmatter before any content
        first_close = text.find("---", 3)
        second_close = text.find("---", first_close + 3)
        # The body between the two --- markers should be valid YAML, not mixed content
        fm_text = text[3:first_close].strip()
        meta_check = yaml.safe_load(fm_text)
        assert isinstance(meta_check, dict), "Frontmatter should parse as a single dict"

        # Verify frontmatter was merged
        end = text.find("---", 3)
        meta = yaml.safe_load(text[3:end].strip()) or {}
        assert meta["confidence"] == 0.9, "Should take higher confidence"
        assert "tag_a" in meta["tags"], "Should include tag_a"
        assert "tag_b" in meta["tags"], "Should include tag_b"
        assert "tool_a" in meta["tools"], "Should include tool_a"
        assert "tool_b" in meta["tools"], "Should include tool_b"
        assert meta["observation_count"] == 2, "Should sum observation counts"

        # Both bodies should be present
        assert "Content A" in text
        assert "Content B" in text

    def test_expire_stale_no_duplicate_keys(self, project_dir):
        """Expiring an already-stale entry should not duplicate the stale key."""
        ensure_project_store(str(project_dir))
        mgr = ProjectManager(Path.home() / ".magnolia")

        path = mgr.create_entry(
            str(project_dir), "Stale Test", "old content",
            confidence=0.5,
        )
        # Set date to 120 days ago
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        mgr._update_entry_frontmatter(Path(path), "last_verified", old_date)
        mgr._update_entry_frontmatter(Path(path), "date", old_date)

        from compchem_memory.learning.consolidator import consolidate_tier
        # First expiry
        consolidate_tier("project", str(project_dir), stale_days=90)
        text1 = Path(path).read_text()
        assert "stale: true" in text1

        # Second expiry should NOT add duplicate stale key
        consolidate_tier("project", str(project_dir), stale_days=90)
        text2 = Path(path).read_text()
        # Count occurrences of "stale:" in frontmatter
        end = text2.find("---", 3)
        fm = text2[3:end]
        assert fm.count("stale:") == 1, "stale key should appear exactly once in frontmatter"


# ── CLI _detect_tool and _detect_run_dir ──────────────────────────────────


class TestDetectTool:
    def test_detects_haddock3(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("haddock3 run.cfg") == "haddock3"

    def test_detects_haddock3_alias(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("run_haddock3 run.cfg") == "haddock3"

    def test_detects_gromacs(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("gmx mdrun -deffnm prod") == "gromacs"

    def test_detects_gnina(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("gnina -r receptor.pdb") == "gnina"

    def test_detects_xtb(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("xtb opt.xyz") == "xtb"

    def test_detects_path_to_tool(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("/usr/local/bin/haddock3 run.cfg") == "haddock3"

    def test_unknown_tool_returns_empty(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("python script.py") == ""

    def test_empty_command_returns_empty(self):
        from compchem_memory.cli import _detect_tool
        assert _detect_tool("") == ""


class TestDetectRunDir:
    def test_detects_runs_path_in_command(self, project_dir):
        from compchem_memory.cli import _detect_run_dir
        run_dir = project_dir / "runs" / "test_run"
        run_dir.mkdir(parents=True)
        # _detect_run_dir checks if the path part itself exists
        result = _detect_run_dir(f"haddock3 {run_dir}", "haddock3")
        assert result is not None
        assert "test_run" in result

    def test_returns_none_for_no_match(self):
        from compchem_memory.cli import _detect_run_dir
        result = _detect_run_dir("haddock3 config.cfg", "haddock3")
        assert result is None
