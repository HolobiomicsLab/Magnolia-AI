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
