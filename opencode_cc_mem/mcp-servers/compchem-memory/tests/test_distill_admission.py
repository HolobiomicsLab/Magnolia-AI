"""Distillation admission unit (exp/distill-admission).

Slice 1 — stub memory-WRITING tool events before they reach the extraction
LLM. Dual capture (explicit memory_record_learning + auto-distiller both
recording the same event) produced the recurring ~16-duplicate-pair batches;
the transcript path already excludes all memory_* outputs
(opencode_ingest._is_memory_plumbing_tool), so the events path is the
remaining vector. Later slices (idle gating, admission prompt gate,
retirement, auto-band consolidation) land in this file too.
"""

import json

from compchem_memory import extraction


def _recording_event(title: str) -> dict:
    return {
        "event_type": "tool_success",
        "tool": "memory_record_learning",
        "args_summary": {"title": title, "content": "SYMPTOMS secret body"},
        "result_summary": '{"status": "created", "title": "' + title + '"}',
    }


def test_stub_replaces_memory_write_payloads():
    events = [
        _recording_event("Duplicate flood root cause Xyzzy42"),
        {"event_type": "tool_success", "tool": "run_shell",
         "args_summary": {"cmd": "ls"}, "result_summary": "fileA Xyzzy42"},
    ]
    out = extraction.AutomaticMemoryExtractor.stub_memory_write_events(events)

    # memory-write event: payloads replaced, occurrence + tool kept
    assert out[0]["tool"] == "memory_record_learning"
    assert out[0]["args_summary"] == extraction.AutomaticMemoryExtractor._MEMORY_WRITE_STUB
    assert out[0]["result_summary"] == extraction.AutomaticMemoryExtractor._MEMORY_WRITE_STUB
    assert "Xyzzy42" not in json.dumps(out[0])
    # non-memory event untouched (original content visible)
    assert out[1]["result_summary"] == "fileA Xyzzy42"
    # caller's originals never mutated
    assert "Xyzzy42" in events[0]["args_summary"]["title"]


def test_stub_covers_session_and_annotate_tools():
    for tool in ("memory_record_session", "memory_annotate"):
        out = extraction.AutomaticMemoryExtractor.stub_memory_write_events(
            [{"tool": tool, "args_summary": {"data": "Quux77"}}])
        assert out[0]["args_summary"] == \
            extraction.AutomaticMemoryExtractor._MEMORY_WRITE_STUB


def test_non_dicts_pass_through():
    out = extraction.AutomaticMemoryExtractor.stub_memory_write_events(
        [{"tool": "run_shell"}, None, "junk"])
    assert out == [{"tool": "run_shell"}, None, "junk"]


def test_llm_distill_sends_stubbed_payload(monkeypatch):
    captured = {}

    def fake(system, user, max_tokens=2000, **kw):
        captured["user"] = user
        return [{"title": "t", "content": "c"}]

    monkeypatch.setattr(extraction, "call_llm_json", fake)
    ext = extraction.AutomaticMemoryExtractor()
    res = ext._llm_distill([_recording_event("Blorptastic91")])
    assert res and res[0]["title"] == "t"
    assert "Blorptastic91" not in captured["user"]
    assert extraction.AutomaticMemoryExtractor._MEMORY_WRITE_STUB in captured["user"]
