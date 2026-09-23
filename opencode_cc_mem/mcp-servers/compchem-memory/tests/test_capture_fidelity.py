"""A1: full-fidelity capture for allowlisted tools + per-record capture-version stamp.

The derived-receipts extractor treats the stamp as load-bearing: v2 records
(full args under ``args``, full result under ``result_summary``/``result``)
can be trusted; v1 records (legacy 5-kwargs/200-char summaries) cannot, and
every cut is flagged — truncation marked, never silent.
"""
import json

import pytest

from compchem_memory import capture as cap
from compchem_memory.capture import captured, reset_registry


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "sessions").mkdir(parents=True)
    return pd


def _read_events(project_dir):
    files = list((project_dir / ".magnolia" / "sessions").glob("*.jsonl"))
    assert len(files) == 1
    return [json.loads(l) for l in files[0].read_text().splitlines()]


@captured(source="compchem-tools")
def submit_job(command, working_dir, scheduler="local", memory="4GB",
               time_limit="24:00:00", note=None, project_dir=None):
    # compchem-tools returns dicts (compchem-memory returns JSON strings)
    return {"success": True, "job_id": "local_1", "run_id": "r_1"}


@captured(source="compchem-memory")
def memory_get_context(query, project_dir=None):
    return "y" * 300


@captured(source="compchem-memory")
def memory_scan_headers(limit=10, project_dir=None):
    return "ok"


def test_allowlisted_tool_full_fidelity(project_dir):
    result = submit_job(
        command="haddock3 cfg", working_dir="/tmp/work", scheduler="slurm",
        memory="32GB", time_limit="48:00:00", note="n" * 500,
        project_dir=str(project_dir),
    )
    events = _read_events(project_dir)
    call, ok = events[1], events[2]

    assert call["capture_version"] == 2
    assert call["args_truncated"] is False
    # all kwargs captured (legacy capped at 5) with no 80-char value cuts
    assert call["args"]["command"] == "haddock3 cfg"
    assert call["args"]["working_dir"] == "/tmp/work"
    assert call["args"]["memory"] == "32GB"
    assert call["args"]["time_limit"] == "48:00:00"
    assert call["args"]["note"] == "n" * 500
    assert call["args_positional"] == []
    assert "memory=32GB" in call["args_summary"]

    assert ok["capture_version"] == 2
    assert ok["result_truncated"] is False
    assert json.loads(ok["result_summary"])["run_id"] == "r_1"  # canonical JSON
    assert ok["result"]["run_id"] == "r_1"                      # structured twin
    assert result["success"] is True                            # decorator still passes through


def test_result_fields_handle_all_result_shapes():
    # compchem-memory style: JSON-string result
    f = cap._result_fields(True, json.dumps({"a": 1}))
    assert f["result"] == {"a": 1} and f["result_summary"] == '{"a": 1}'
    # compchem-tools style: dict result
    f = cap._result_fields(True, {"a": 1})
    assert f["result"] == {"a": 1} and f["result_summary"] == '{"a": 1}'
    # plain-text result: no structured twin, summary passes through
    f = cap._result_fields(True, "plain text")
    assert "result" not in f and f["result_summary"] == "plain text"


def test_non_allowlisted_stays_v1_and_flags_cuts(project_dir):
    memory_get_context("q" * 100, project_dir=str(project_dir))
    events = _read_events(project_dir)
    call, ok = events[1], events[2]

    assert call["capture_version"] == 1
    assert call["args_truncated"] is True          # 100-char value cut at 80
    assert "q" * 100 not in call["args_summary"]
    assert call["args_summary"].count("q") == 80   # exactly the legacy 80-char cut
    assert "args" not in call                      # structured args is v2-only

    assert ok["capture_version"] == 1
    assert ok["result_truncated"] is True
    assert ok["result_summary"].endswith("...")
    assert len(ok["result_summary"]) == 203        # 200 + "..."
    assert "result" not in ok


def test_non_allowlisted_untruncated_flags_false(project_dir):
    memory_scan_headers(project_dir=str(project_dir))
    events = _read_events(project_dir)
    assert events[1]["args_truncated"] is False
    assert events[2]["result_truncated"] is False


def test_v2_safety_cap_is_flagged(project_dir, monkeypatch):
    monkeypatch.setattr(cap, "V2_RESULT_CAP", 40)
    submit_job("c", "/w", project_dir=str(project_dir))
    ok = _read_events(project_dir)[2]
    assert ok["capture_version"] == 2
    assert ok["result_truncated"] is True
    assert len(ok["result_summary"]) == 40
    assert "result" not in ok   # structured twin omitted when over the cap


@captured(source="compchem-memory")
def failing_tool(project_dir=None):
    raise ValueError("boom " * 200)


def test_error_event_stamped_and_flagged(project_dir):
    with pytest.raises(ValueError):
        failing_tool(project_dir=str(project_dir))
    err = _read_events(project_dir)[2]
    assert err["capture_version"] == 1
    assert err["error_truncated"] is True
    assert "boom" in err["error"]


def test_existing_consumers_accept_v2_records(project_dir):
    """The 3 JSONL consumers (extraction helpers, compaction builders) read
    event_type/tool/result_summary — v2's extra keys must be inert."""
    for _ in range(3):  # >= MIN_MESSAGES_TO_KEEP(5) events after the header
        submit_job(command="haddock3 cfg", working_dir="/tmp/work",
                   scheduler="slurm", memory="32GB", time_limit="48:00:00",
                   project_dir=str(project_dir))
    events = _read_events(project_dir)

    from compchem_memory.extraction import has_error_fix_pattern
    assert has_error_fix_pattern(events) is False

    from compchem_memory.compaction import (
        _extract_compaction_notes,
        _try_session_memory_compact,
    )
    notes = _extract_compaction_notes(events)
    assert "submit_job: 3x" in notes
    assert "[submit_job]" in notes

    compacted = _try_session_memory_compact(events, threshold=100_000)
    assert compacted is not None
    assert compacted.tokens_before > 0


def test_manifest_is_the_single_allowlist_source():
    from compchem_memory.capture_manifest import (
        FULL_FIDELITY_TOOLS,
        TOOL_CLASSIFICATIONS,
    )
    assert FULL_FIDELITY_TOOLS == frozenset(
        name for name, klass in TOOL_CLASSIFICATIONS.items()
        if klass == "orchestration"
    )
    assert "submit_job" in FULL_FIDELITY_TOOLS
