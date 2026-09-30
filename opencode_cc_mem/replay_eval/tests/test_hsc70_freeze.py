"""Unit tests for the hsc70 bake-off freeze helpers (no LLM, no DB)."""

from __future__ import annotations

import json
from pathlib import Path

from replay_eval.hsc70_freeze import (
    compute_rent,
    is_clean_title,
    parse_get_context_sources,
    store_entry_ids,
)


def _part(output_obj: dict, tool: str = "compchem-memory_memory_get_context",
          status: str = "completed") -> str:
    return json.dumps({
        "type": "tool",
        "tool": tool,
        "state": {"status": status, "output": json.dumps(output_obj)},
    })


def test_parse_sources_happy_path():
    data = _part({"content": "x", "sources": [
        {"tier": "project", "id": "a.md"},
        {"tier": "staging", "id": "b.md"},
    ]})
    got = parse_get_context_sources(data)
    assert [s["id"] for s in got] == ["a.md", "b.md"]


def test_parse_sources_ignores_non_result_rows():
    call_part = json.dumps({
        "type": "tool", "tool": "compchem-memory_memory_get_context",
        "state": {"status": "pending", "input": {"task_description": "x"}},
    })
    other_tool = _part({"sources": [{"id": "a.md"}]}, tool="read")
    bad_json = "{not json"
    for row in (call_part, other_tool, bad_json):
        assert parse_get_context_sources(row) == []


def test_parse_sources_drops_idless_entries():
    data = _part({"sources": [{"tier": "goal"}, {"tier": "run", "id": "r1"}, "junk"]})
    assert parse_get_context_sources(data) == [{"tier": "run", "id": "r1"}]


def test_is_clean_title_filters_sandbox_and_replay():
    assert is_clean_title("Docking hsc70 with KFERQ peptide") is True
    for bad in ("Sandbox replay of hsc70 store", "slice-validation pass 2",
                "2026-09-14 bake-off arm B", "Distill-admission evidence"):
        assert is_clean_title(bad) is False


def test_compute_rent_counts_distinct_sessions_and_filters_ids():
    valid = {"a.md", "b.md"}
    rows = [
        ("s1", _part({"sources": [{"id": "a.md"}]})),
        ("s1", _part({"sources": [{"id": "a.md"}]})),  # same session twice = 1
        ("s2", _part({"sources": [{"id": "a.md"}, {"id": "foreign.md"}]})),
        ("s3", _part({"sources": [{"id": "b.md"}]})),
    ]
    rent = compute_rent(rows, {"s1": "t", "s2": "t", "s3": "t"}, valid)
    assert rent["entries"]["a.md"]["distinct"] == 2
    assert rent["entries"]["b.md"]["distinct"] == 1
    assert "foreign.md" not in rent["entries"]
    assert rent["workhorses"] == []  # nothing reaches 3


def test_compute_rent_workhorse_threshold_three_sessions():
    valid = {"a.md"}
    rows = [(s, _part({"sources": [{"id": "a.md"}]})) for s in ("s1", "s2", "s3")]
    rent = compute_rent(rows, {}, valid)
    assert rent["workhorses"] == ["a.md"]


def test_compute_rent_excludes_dirty_sessions_when_clean_only():
    valid = {"a.md"}
    titles = {"s1": "normal work", "s2": "sandbox replay"}
    rows = [
        ("s1", _part({"sources": [{"id": "a.md"}]})),
        ("s2", _part({"sources": [{"id": "a.md"}]})),
        ("s2", _part({"sources": [{"id": "a.md"}]})),
    ]
    clean = compute_rent(rows, titles, valid, clean_only=True)
    raw = compute_rent(rows, titles, valid, clean_only=False)
    assert clean["entries"]["a.md"]["distinct"] == 1
    assert raw["entries"]["a.md"]["distinct"] == 2
    assert "s2" in clean["excluded_sessions"]


def test_never_surfaced_lists_valid_unsurfaced():
    valid = {"a.md", "z.md"}
    rows = [("s1", _part({"sources": [{"id": "a.md"}]}))]
    rent = compute_rent(rows, {"s1": "t"}, valid)
    assert rent["never_surfaced"] == ["z.md"]


def test_store_entry_ids_reads_entries_and_staging(tmp_path):
    (tmp_path / "entries").mkdir()
    (tmp_path / "staging").mkdir()
    (tmp_path / "entries" / "e1.md").write_text("x")
    (tmp_path / "staging" / "s1.md").write_text("x")
    (tmp_path / "runs" / "r.yaml").parent.mkdir(parents=True)
    (tmp_path / "runs" / "r.yaml").write_text("x")
    assert store_entry_ids(tmp_path) == {"e1.md", "s1.md"}
