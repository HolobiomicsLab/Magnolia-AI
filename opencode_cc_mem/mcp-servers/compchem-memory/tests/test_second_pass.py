"""Additive second distillation pass (MAGNOLIA_DISTILL_SECOND_PASS).

Pass 2 shows the model its own pass-1 candidates after the transcript and asks
only for what is missing (Cadd arm, runs/2026-09-14_slice-validation). Union is
deduped locally against pass 1 (conjunction rule, pure numbers ignored) with
drops logged to <project>/.magnolia/distill-second-pass.jsonl. A pass-2 parse
failure never loses pass 1.
"""

import json
from pathlib import Path

import pytest

from compchem_memory import extraction

PASS1 = [{"type": "scientific_finding", "title": "Contact map shows F2 near R272",
          "content": "c", "tags": [], "tools": [], "confidence": 0.7}]
NEW = {"type": "note", "title": "ThS/NBD competitive probe finding",
       "content": "c", "tags": [], "tools": [], "confidence": 0.6}
DUP = {"type": "note", "title": "the contact map shows peptide F2 near R272",
       "content": "c", "tags": [], "tools": [], "confidence": 0.6}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _patch_llm(monkeypatch, responses):
    calls = []

    def fake(system, user, max_tokens=2000, **kw):
        calls.append({"system": system, "user": user})
        return responses[len(calls) - 1] if len(calls) <= len(responses) else []

    monkeypatch.setattr(extraction, "call_llm_json", fake)
    return calls


def _telemetry_rows(tmp_path):
    f = tmp_path / ".magnolia" / "distill-second-pass.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def test_flag_off_single_call(monkeypatch, store):
    monkeypatch.delenv(extraction.SECOND_PASS_ENV, raising=False)
    calls = _patch_llm(monkeypatch, [PASS1])
    ext = extraction.AutomaticMemoryExtractor()
    assert ext.distill_transcript("transcript text") == PASS1
    assert len(calls) == 1
    assert _telemetry_rows(store) == []


def test_flag_on_union_dedups_against_pass1(monkeypatch, store):
    monkeypatch.setenv(extraction.SECOND_PASS_ENV, "1")
    calls = _patch_llm(monkeypatch, [PASS1, [NEW, DUP]])
    ext = extraction.AutomaticMemoryExtractor()
    out = ext.distill_transcript("transcript text")
    assert len(calls) == 2
    # pass-2 user message contains transcript + the already-extracted list
    assert "ALREADY EXTRACTED IN A FIRST PASS" in calls[1]["user"]
    assert "Contact map shows F2 near R272" in calls[1]["user"]
    assert out == PASS1 + [NEW]                       # duplicate dropped
    rows = _telemetry_rows(store)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["pass1"] == 1
    assert rows[0]["added"] == 1
    assert rows[0]["dedup_dropped"] == 1


def test_parse_failure_returns_pass1(monkeypatch, store):
    monkeypatch.setenv(extraction.SECOND_PASS_ENV, "1")
    _patch_llm(monkeypatch, [PASS1, None])
    ext = extraction.AutomaticMemoryExtractor()
    assert ext.distill_transcript("transcript text") == PASS1
    rows = _telemetry_rows(store)
    assert rows[0]["outcome"] == "parse_fail"
    assert rows[0]["added"] == 0


def test_non_list_returns_pass1(monkeypatch, store):
    monkeypatch.setenv(extraction.SECOND_PASS_ENV, "1")
    _patch_llm(monkeypatch, [PASS1, {"oops": True}])
    ext = extraction.AutomaticMemoryExtractor()
    assert ext.distill_transcript("transcript text") == PASS1
    assert _telemetry_rows(store)[0]["outcome"] == "not_list"


def test_numeric_only_tokens_do_not_block_new_title(monkeypatch, store):
    """A1 dedup lesson: pure numbers (run ids, residues) must not cause a
    spurious duplicate match between genuinely different titles."""
    monkeypatch.setenv(extraction.SECOND_PASS_ENV, "1")
    p1 = [{"type": "workflow_note", "title": "run 1234 finished clean",
           "content": "c", "tags": [], "tools": [], "confidence": 0.6}]
    p2 = [{"type": "workflow_note", "title": "run 9876 failed on timeout",
           "content": "c", "tags": [], "tools": [], "confidence": 0.6}]
    _patch_llm(monkeypatch, [p1, p2])
    ext = extraction.AutomaticMemoryExtractor()
    out = ext.distill_transcript("transcript text")
    assert out == p1 + p2


def test_no_project_dir_telemetry_is_noop(monkeypatch):
    monkeypatch.delenv("MAGNOLIA_PROJECT_DIR", raising=False)
    monkeypatch.setenv(extraction.SECOND_PASS_ENV, "1")
    _patch_llm(monkeypatch, [PASS1, []])
    ext = extraction.AutomaticMemoryExtractor()          # must not raise
    assert ext.distill_transcript("t") == PASS1


def test_flag_values(monkeypatch):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv(extraction.SECOND_PASS_ENV, v)
        assert extraction._second_pass_enabled() is True
    for v in ("0", "", "garbage", "off"):
        monkeypatch.setenv(extraction.SECOND_PASS_ENV, v)
        assert extraction._second_pass_enabled() is False
    monkeypatch.delenv(extraction.SECOND_PASS_ENV, raising=False)
    assert extraction._second_pass_enabled() is False
