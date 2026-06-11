"""scan_and_distill dispatch: dialogue is primary, tool-event is fallback-only.

Normal mode (mapping + LLM + opencode) distils the dialogue transcript and does
NOT also run the tool-event heuristic extractor — one distillation per session.
The tool-event path runs only when the dialogue path cannot.
"""

import json
from pathlib import Path

import pytest

from compchem_memory import startup_scan


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    for sub in ["sessions", "staging", "entries"]:
        (pd / ".magnolia" / sub).mkdir(parents=True)
    return pd


def _mapping(pd, ids):
    (pd / ".magnolia" / "opencode-sessions.jsonl").write_text(
        "".join(json.dumps({"opencode_session_id": i, "ts": "t"}) + "\n" for i in ids))


def _session(pd, name, events):
    (pd / ".magnolia" / "sessions" / f"{name}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")


def _track(monkeypatch):
    called = {"ingest": 0, "commit": 0}
    monkeypatch.setattr(
        "compchem_memory.opencode_ingest.ingest_opencode_sessions",
        lambda *a, **k: (called.__setitem__("ingest", called["ingest"] + 1) or []),
    )
    from compchem_memory import extraction
    monkeypatch.setattr(
        extraction.AutomaticMemoryExtractor, "commit",
        lambda self, *a, **k: (called.__setitem__("commit", called["commit"] + 1) or []),
    )
    return called


def test_dialogue_primary_runs_and_skips_tool_events(project_dir, monkeypatch):
    _mapping(project_dir, ["ses_a"])
    _session(project_dir, "2026-05-10_000000", [{"event_type": "tool_call", "tool": "x"}])
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    monkeypatch.setattr(startup_scan, "_opencode_available", lambda: True)
    called = _track(monkeypatch)

    result = startup_scan.scan_and_distill(str(project_dir))

    assert called["ingest"] == 1
    assert called["commit"] == 0
    assert result["mode"] == "dialogue"


def test_falls_back_to_tool_events_when_no_llm(project_dir, monkeypatch):
    _mapping(project_dir, ["ses_a"])
    _session(project_dir, "2026-05-10_000000", [{"event_type": "tool_call", "tool": "x"}])
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: False)
    monkeypatch.setattr(startup_scan, "_opencode_available", lambda: True)
    called = _track(monkeypatch)

    result = startup_scan.scan_and_distill(str(project_dir))

    assert called["ingest"] == 0
    assert called["commit"] == 1
    assert result["mode"] == "tool_event"


def test_falls_back_when_no_opencode_binary(project_dir, monkeypatch):
    _mapping(project_dir, ["ses_a"])
    _session(project_dir, "2026-05-10_000000", [{"event_type": "tool_call", "tool": "x"}])
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    monkeypatch.setattr(startup_scan, "_opencode_available", lambda: False)
    called = _track(monkeypatch)

    result = startup_scan.scan_and_distill(str(project_dir))

    assert called["ingest"] == 0
    assert called["commit"] == 1
    assert result["mode"] == "tool_event"


def test_falls_back_when_no_mapping(project_dir, monkeypatch):
    _session(project_dir, "2026-05-10_000000", [{"event_type": "tool_call", "tool": "x"}])
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    monkeypatch.setattr(startup_scan, "_opencode_available", lambda: True)
    called = _track(monkeypatch)

    result = startup_scan.scan_and_distill(str(project_dir))

    assert called["ingest"] == 0
    assert called["commit"] == 1
    assert result["mode"] == "tool_event"
