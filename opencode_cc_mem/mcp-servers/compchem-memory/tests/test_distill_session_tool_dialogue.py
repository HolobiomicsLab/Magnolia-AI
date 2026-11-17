"""memory_distill_session distils the active session's DIALOGUE transcript when
the dialogue path is available, falling back to the tool-event log otherwise.
"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path, monkeypatch):
    from compchem_memory import server
    from compchem_memory import opencode_ingest as oi
    from compchem_memory.storage import ensure_project_store
    from compchem_memory.capture import reset_registry

    reset_registry()
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    monkeypatch.setattr(server, "PROJECT_DIR", str(pd))

    # mapping naming the live session
    (pd / ".magnolia" / "opencode-sessions.jsonl").write_text(
        json.dumps({"opencode_session_id": "ses_live", "ts": "t"}) + "\n")

    # dialogue path available
    monkeypatch.setattr(server, "is_llm_available", lambda: True)
    monkeypatch.setattr(server, "_opencode_available", lambda: True)

    # fake transcript export + distiller
    monkeypatch.setattr(oi, "export_session", lambda sid: {
        "info": {"id": sid},
        "messages": [{"info": {"id": "m1", "role": "user"},
                      "parts": [{"type": "text", "text": "DIALOGUE_MARKER finding"}]}],
    })
    monkeypatch.setattr(oi, "_default_distiller",
                        lambda t: [{"title": "Dlg", "content": t, "type": "scientific_finding"}])

    yield pd
    reset_registry()


def _tool(server_module):
    fn = server_module.memory_distill_session
    return getattr(fn, "fn", fn)


def test_preview_uses_dialogue_and_saves_nothing(project):
    from compchem_memory import server
    distill = _tool(server)

    payload = json.loads(distill(project_dir=str(project)))
    assert payload["status"] == "preview"
    assert payload["candidate_count"] >= 1
    assert "DIALOGUE_MARKER" in json.dumps(payload["candidates"])
    assert list((project / ".magnolia" / "staging").glob("*.md")) == []


def test_commit_saves_dialogue_and_advances_cursor(project):
    from compchem_memory import server
    distill = _tool(server)

    payload = json.loads(distill(commit=True, project_dir=str(project)))
    assert payload["status"] == "committed"
    assert payload["saved_count"] >= 1
    assert len(list((project / ".magnolia" / "staging").glob("*.md"))) >= 1
    rec = json.loads((project / ".magnolia" / "opencode-distilled" / "ses_live.json").read_text())
    assert rec["cursor"] == "m1"


def test_commit_refuses_dialogue_retry_when_llm_route_is_unavailable(
    project, monkeypatch,
):
    from compchem_memory import opencode_ingest as oi
    from compchem_memory import server

    marker = project / ".magnolia" / "opencode-distilled" / "ses_live.json"
    marker.parent.mkdir(parents=True)
    initial_progress = {
        "cursor": None,
        "candidates": 0,
        "done": False,
        "retry": {"attempts": 5, "next_retry_at": None, "exhausted": True},
    }
    marker.write_text(json.dumps(initial_progress) + "\n")
    distiller_calls = []
    monkeypatch.setattr(server, "is_llm_available", lambda: False)
    monkeypatch.setattr(oi, "is_llm_available", lambda: False, raising=False)
    monkeypatch.setattr(
        oi, "_default_distiller", lambda text: distiller_calls.append(text) or [],
    )

    payload = json.loads(_tool(server)(commit=True, project_dir=str(project)))

    assert payload["status"] == "skipped"
    assert payload["reason"] == "configured memory LLM is unavailable"
    assert payload["retry"] is False
    assert payload["source"] == "dialogue"
    assert distiller_calls == []
    assert json.loads(marker.read_text()) == initial_progress
    assert list((project / ".magnolia" / "staging").glob("*.md")) == []


def test_commit_respects_dialogue_active_retry_backoff(project, monkeypatch):
    from compchem_memory import opencode_ingest as oi
    from compchem_memory import server

    scheduled_at = "2099-01-02T03:04:05+00:00"
    marker = project / ".magnolia" / "opencode-distilled" / "ses_live.json"
    marker.parent.mkdir(parents=True)
    initial_progress = {
        "cursor": None,
        "candidates": 0,
        "done": False,
        "retry": {
            "attempts": 2,
            "next_retry_at": scheduled_at,
            "exhausted": False,
        },
    }
    marker.write_text(json.dumps(initial_progress) + "\n")
    distiller_calls = []
    monkeypatch.setattr(
        oi, "_default_distiller", lambda text: distiller_calls.append(text) or [],
    )

    payload = json.loads(_tool(server)(commit=True, project_dir=str(project)))

    assert payload["status"] == "skipped"
    assert payload["reason"] == f"extraction retry scheduled at {scheduled_at}"
    assert payload["retry"] is False
    assert payload["next_retry_at"] == scheduled_at
    assert payload["source"] == "dialogue"
    assert distiller_calls == []
    assert json.loads(marker.read_text()) == initial_progress


def test_hook_session_uses_dialogue_distillation_without_opencode(tmp_path, monkeypatch):
    from compchem_memory import opencode_ingest as oi
    from compchem_memory import server
    from compchem_memory.storage import ensure_project_store

    pd = tmp_path / "hook-project"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"
    (store / "agent-sessions.jsonl").write_text(json.dumps({
        "ts": "2026-11-15T10:00:00Z",
        "harness": "claude",
        "session_id": "claude-one",
    }) + "\n")
    transcript = store / "transcripts" / "claude" / "claude-one.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "kind": "prompt", "text": "HOOK_DIALOGUE_MARKER",
    }) + "\n")
    monkeypatch.setattr(server, "is_llm_available", lambda: True)
    monkeypatch.setattr(server, "_opencode_available", lambda: False)
    monkeypatch.setattr(oi, "_default_distiller", lambda text: [{
        "title": "Hook dialogue", "content": text, "type": "scientific_finding",
    }])

    payload = json.loads(_tool(server)(project_dir=str(pd)))

    assert payload["status"] == "preview"
    assert payload["source"] == "dialogue"
    assert "HOOK_DIALOGUE_MARKER" in json.dumps(payload["candidates"])


def test_distill_prefers_resumed_active_opencode_by_activity(tmp_path, monkeypatch):
    import os
    from datetime import datetime, timezone

    from compchem_memory import server
    from compchem_memory.storage import ensure_project_store

    pd = tmp_path / "mixed-project"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"
    (store / "opencode-sessions.jsonl").write_text(json.dumps({
        "ts": "2026-11-15T09:00:00Z", "opencode_session_id": "ses-a",
    }) + "\n")
    opencode = store / "opencode-transcripts" / "ses-a.json"
    opencode.parent.mkdir(exist_ok=True)
    opencode.write_text('{"messages": []}')
    (store / "agent-sessions.jsonl").write_text(json.dumps({
        "ts": "2026-11-15T09:30:00Z",
        "harness": "claude",
        "session_id": "claude-one",
    }) + "\n")
    claude = store / "transcripts" / "claude" / "claude-one.jsonl"
    claude.parent.mkdir(parents=True)
    claude.write_text(json.dumps({"kind": "prompt", "text": "hello"}) + "\n")

    def timestamp(hour, minute=0):
        return datetime(2026, 11, 15, hour, minute, tzinfo=timezone.utc).timestamp()

    os.utime(claude, (timestamp(10), timestamp(10)))
    os.utime(opencode, (timestamp(10, 30), timestamp(10, 30)))
    selected = []
    monkeypatch.setattr(server, "is_llm_available", lambda: True)
    monkeypatch.setattr(server, "_opencode_available", lambda: True)
    monkeypatch.setattr(server, "distill_session_transcript", lambda store, ref, **kwargs: (
        selected.append(ref.key) or {"status": "preview", "candidates": []}
    ))

    _tool(server)(project_dir=str(pd))
    assert selected == ["ses-a"]

    os.utime(claude, (timestamp(11), timestamp(11)))
    _tool(server)(project_dir=str(pd))
    assert selected == ["ses-a", "claude-claude-one"]
