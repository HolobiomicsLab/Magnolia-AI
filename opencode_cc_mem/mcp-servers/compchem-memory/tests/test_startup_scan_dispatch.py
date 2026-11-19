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
    from compchem_memory import llm

    _mapping(project_dir, ["ses_a"])
    _session(project_dir, "2026-05-10_000000", [{"event_type": "tool_call", "tool": "x"}])
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: False)
    monkeypatch.setattr(llm, "resolve_route", lambda _role: None)
    monkeypatch.setattr(startup_scan, "_opencode_available", lambda: True)
    called = _track(monkeypatch)
    drained = []
    monkeypatch.setattr(
        "compchem_memory.hook.background.drain_opencode_payloads",
        lambda store, **kwargs: drained.append((Path(store), kwargs)),
    )

    result = startup_scan.scan_and_distill(str(project_dir))

    assert called["ingest"] == 0
    assert called["commit"] == 1
    assert result["mode"] == "tool_event"
    assert drained == [(project_dir / ".magnolia", {"critic_skip_reason": "no route"})]


def test_independent_critic_route_drains_backlog_when_memory_route_is_unavailable(
    project_dir, monkeypatch,
):
    from compchem_memory import llm

    _session(project_dir, "2026-05-10_000000", [{"event_type": "tool_call", "tool": "x"}])
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: False)
    monkeypatch.setattr(
        llm,
        "resolve_route",
        lambda role: object() if role == "critic" else None,
    )
    _track(monkeypatch)
    drained = []
    monkeypatch.setattr(
        "compchem_memory.hook.background.drain_opencode_payloads",
        lambda store, **kwargs: drained.append((Path(store), kwargs)),
    )

    startup_scan.scan_and_distill(str(project_dir))

    assert drained == [(project_dir / ".magnolia", {})]


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


def test_startup_scan_dialogue_for_hook_sessions_without_opencode(
    project_dir, monkeypatch,
):
    store = project_dir / ".magnolia"
    (store / "agent-sessions.jsonl").write_text(json.dumps({
        "ts": "2026-11-15T10:00:00Z",
        "harness": "claude",
        "session_id": "claude-one",
        "transcript_ref": None,
    }) + "\n")
    transcript = store / "transcripts" / "claude" / "claude-one.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"kind": "prompt", "text": "hello"}) + "\n")
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    monkeypatch.setattr(startup_scan, "_opencode_available", lambda: False)
    called = _track(monkeypatch)

    result = startup_scan.scan_and_distill(str(project_dir))

    assert called["ingest"] == 1
    assert called["commit"] == 0
    assert result["mode"] == "dialogue"


def test_hook_source_check_does_not_rescan_session_registry(project_dir, monkeypatch):
    store = project_dir / ".magnolia"
    (store / "agent-sessions.jsonl").write_text(json.dumps({
        "ts": "2026-11-15T10:00:00Z",
        "harness": "claude",
        "session_id": "claude-one",
    }) + "\n")
    transcript = store / "transcripts" / "claude" / "claude-one.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"kind": "prompt", "text": "hello"}) + "\n")
    monkeypatch.setattr(
        "compchem_memory.transcript_source.resolve_transcript",
        lambda *args: (_ for _ in ()).throw(AssertionError("registry rescanned")),
    )

    assert startup_scan._has_dialogue_source(store) is True
