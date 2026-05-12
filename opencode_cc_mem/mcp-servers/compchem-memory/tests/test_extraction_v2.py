"""Tests for extraction v2: error→fix shortcut, significant-result heuristic, prompt v2."""

import json
import tempfile
from pathlib import Path

import pytest

from compchem_memory.extraction import (
    AutomaticMemoryExtractor,
    has_error_fix_pattern,
    has_significant_result,
    EXTRACTION_SYSTEM_PROMPT,
)


def _make_session_file(tmp_path, events):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def test_has_error_fix_pattern_detects_error_then_success(tmp_path):
    events = [
        {"event_type": "session_start", "session_id": "s1", "project_id": "p1", "schema_version": 2},
        {"event_type": "tool_error", "tool": "haddock3", "error": "missing"},
        {"event_type": "tool_success", "tool": "haddock3", "result_summary": "ok"},
    ]
    assert has_error_fix_pattern(events) is True


def test_has_error_fix_pattern_ignores_unrelated_tools(tmp_path):
    events = [
        {"event_type": "tool_error", "tool": "haddock3", "error": "x"},
        {"event_type": "tool_success", "tool": "gnina", "result_summary": "ok"},
    ]
    assert has_error_fix_pattern(events) is False


def test_has_significant_result_clean_pass(tmp_path):
    events = [
        {"event_type": "run_assessment", "tool": "haddock3", "overall": "pass", "quality_flags": []},
    ]
    assert has_significant_result(events, str(tmp_path)) is True


def test_has_significant_result_pass_with_flags_does_not_trigger_clean_branch(tmp_path):
    events = [
        {"event_type": "run_assessment", "tool": "haddock3", "overall": "pass",
         "quality_flags": ["low_cluster_count"]},
    ]
    assert has_significant_result(events, str(tmp_path)) is False


def test_has_significant_result_finally_got_it_working(tmp_path):
    events = [
        {"event_type": "tool_error", "tool": "haddock3", "error": "x"},
        {"event_type": "tool_error", "tool": "haddock3", "error": "y"},
        {"event_type": "tool_success", "tool": "haddock3", "result_summary": "ok"},
        {"event_type": "run_assessment", "tool": "haddock3", "overall": "pass",
         "quality_flags": ["minor"]},
    ]
    assert has_significant_result(events, str(tmp_path)) is True


def test_should_extract_fires_on_error_fix_under_threshold(tmp_path):
    """Error→fix shortcut triggers should_extract even when tokens are far below 5000."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    extractor = AutomaticMemoryExtractor(str(state_dir))
    session_path = _make_session_file(
        tmp_path,
        [
            {"event_type": "session_start", "session_id": "s1", "project_id": "p", "schema_version": 2},
            {"event_type": "tool_error", "tool": "haddock3", "error": "missing topology"},
            {"event_type": "tool_success", "tool": "haddock3", "result_summary": "ok"},
        ],
    )
    assert extractor.should_extract(session_path) is True


def test_should_extract_false_when_no_pattern_and_below_threshold(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    extractor = AutomaticMemoryExtractor(str(state_dir))
    session_path = _make_session_file(
        tmp_path,
        [
            {"event_type": "session_start", "session_id": "s1", "project_id": "p", "schema_version": 2},
            {"event_type": "tool_call", "tool": "haddock3"},
        ],
    )
    assert extractor.should_extract(session_path) is False


def test_distill_not_called_when_should_extract_false(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    extractor = AutomaticMemoryExtractor(str(state_dir))
    session_path = _make_session_file(
        tmp_path,
        [{"event_type": "tool_call", "tool": "x"}],
    )

    calls = {"count": 0}
    def fake_distill(events):
        calls["count"] += 1
        return []
    monkeypatch.setattr(extractor, "_llm_distill", fake_distill)

    assert extractor.should_extract(session_path) is False
    assert calls["count"] == 0


def test_dual_coding_both_heuristics_fire_on_overlapping_pattern(tmp_path):
    """An event sequence with tool_error → tool_success → run_assessment(pass, clean)
    triggers BOTH has_error_fix_pattern and has_significant_result."""
    events = [
        {"event_type": "session_start", "session_id": "s1", "project_id": "p", "schema_version": 2},
        {"event_type": "tool_error", "tool": "haddock3", "error": "missing"},
        {"event_type": "tool_success", "tool": "haddock3", "result_summary": "ok"},
        {"event_type": "run_assessment", "tool": "haddock3", "overall": "pass", "quality_flags": []},
    ]
    assert has_error_fix_pattern(events) is True
    assert has_significant_result(events, str(tmp_path)) is True


def test_prompt_v2_marker_present():
    assert "prompt_version: v2.0" in EXTRACTION_SYSTEM_PROMPT


def test_prompt_v2_mandatory_sections_present():
    for marker in ("Quantitative grounding", "CAVEAT:", "Symptoms:", "Cause:",
                   "Fix:", "failure_pattern", "USE THE MOST SPECIFIC TYPE"):
        assert marker in EXTRACTION_SYSTEM_PROMPT, f"missing marker: {marker!r}"
