"""Heuristic extraction must skip bare 'exit=N' errors.

Background: 2026-05-30 audit found 115/118 staging entries were content-free
auto-extractions of shape 'Resolved: exit=1' / 'Unresolved failure: exit=141'.
These pin the no-noise contract.
"""
from compchem_memory.extraction import (
    AutomaticMemoryExtractor,
    _is_meaningful_error,
)


def test_is_meaningful_error_rejects_bare_exit():
    assert _is_meaningful_error("exit=1") is False
    assert _is_meaningful_error("exit=2") is False
    assert _is_meaningful_error("exit=141") is False
    assert _is_meaningful_error("exit=255") is False
    assert _is_meaningful_error("EXIT=0") is False        # case-insensitive
    assert _is_meaningful_error("  exit = 7  ") is False  # whitespace tolerated
    assert _is_meaningful_error("") is False
    assert _is_meaningful_error("   ") is False
    assert _is_meaningful_error(None) is False
    assert _is_meaningful_error("Unknown error") is False


def test_is_meaningful_error_accepts_real_content():
    assert _is_meaningful_error("ConnectionError: refused") is True
    assert _is_meaningful_error("ssh: connect to host azzurra port 22: connection timed out") is True
    assert _is_meaningful_error("exit=1\nstderr: file not found") is True   # multiline → has content
    assert _is_meaningful_error("exit=N") is True                          # weird shape but informative
    assert _is_meaningful_error("permission denied") is True


def test_find_error_resolutions_skips_bare_exit_pairs(tmp_path):
    """error_resolution candidates must not be produced from 'exit=N' alone."""
    ex = AutomaticMemoryExtractor(str(tmp_path))
    events = [
        {"event_type": "tool_error",   "tool": "shell", "error": "exit=1"},
        {"event_type": "tool_success", "tool": "shell", "result_summary": "ok"},
        {"event_type": "tool_error",   "tool": "shell", "error": "exit=141"},
        {"event_type": "tool_success", "tool": "shell", "result_summary": "ok"},
    ]
    pairs = ex._find_error_resolutions(events)
    assert pairs == []   # all bare exit=N → all filtered


def test_find_error_resolutions_keeps_real_pairs(tmp_path):
    ex = AutomaticMemoryExtractor(str(tmp_path))
    events = [
        {"event_type": "tool_error",   "tool": "haddock3", "error": "ssh: connection refused"},
        {"event_type": "tool_success", "tool": "haddock3", "result_summary": "completed"},
    ]
    pairs = ex._find_error_resolutions(events)
    assert len(pairs) == 1
    assert pairs[0][0] == "ssh: connection refused"


def test_find_error_resolutions_mixed_keeps_only_meaningful(tmp_path):
    ex = AutomaticMemoryExtractor(str(tmp_path))
    events = [
        {"event_type": "tool_error",   "tool": "shell", "error": "exit=1"},
        {"event_type": "tool_success", "tool": "shell", "result_summary": "ok"},
        {"event_type": "tool_error",   "tool": "haddock3", "error": "FileNotFoundError: input.pdb"},
        {"event_type": "tool_success", "tool": "haddock3", "result_summary": "completed"},
    ]
    pairs = ex._find_error_resolutions(events)
    assert len(pairs) == 1
    assert "FileNotFoundError" in pairs[0][0]


def test_find_unresolved_failures_skips_bare_exit(tmp_path):
    ex = AutomaticMemoryExtractor(str(tmp_path))
    events = [
        {"event_type": "tool_error", "tool": "shell", "error": "exit=1"},
        # no subsequent success → would otherwise produce an unresolved-failure entry
    ]
    failures = ex._find_unresolved_failures(events)
    assert failures == []


def test_find_unresolved_failures_keeps_real_errors(tmp_path):
    ex = AutomaticMemoryExtractor(str(tmp_path))
    events = [
        {"event_type": "tool_error", "tool": "haddock3",
         "error": "CNS topology generation failed for residue ABC"},
    ]
    failures = ex._find_unresolved_failures(events)
    assert len(failures) == 1
    assert "CNS topology" in failures[0]["error"]
