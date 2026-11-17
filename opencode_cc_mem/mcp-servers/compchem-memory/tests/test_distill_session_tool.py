"""Tests for memory_distill_session: preview default, commit flag."""

import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def project(tmp_path, monkeypatch):
    from compchem_memory import server
    from compchem_memory.storage import ensure_project_store
    from compchem_memory.capture import get_session_manager, reset_registry

    reset_registry()
    monkeypatch.setattr(server, "_extractor", None)
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    monkeypatch.setattr(server, "PROJECT_DIR", str(pd))

    # Create an active session log with an error->fix pattern
    mgr = get_session_manager(str(pd))
    mgr.record("tool_error", {"tool": "haddock3", "error": "missing topology"})
    mgr.record("tool_success", {"tool": "haddock3", "result_summary": "ran ok"})

    # Force heuristic path for determinism
    from compchem_memory import extraction
    monkeypatch.setattr(extraction, "is_llm_available", lambda: False)

    yield pd
    monkeypatch.setattr(server, "_extractor", None)
    reset_registry()


def _tool(server_module):
    fn = server_module.memory_distill_session
    return getattr(fn, "fn", fn)


def test_preview_default_saves_nothing(project):
    from compchem_memory import server
    distill = _tool(server)

    result = distill(project_dir=str(project))
    payload = json.loads(result)
    assert payload["status"] == "preview"
    assert payload["candidate_count"] >= 1
    assert list((project / ".magnolia" / "staging").glob("*.md")) == [], \
        "preview must not save"


def test_commit_true_saves(project):
    from compchem_memory import server
    distill = _tool(server)

    result = distill(commit=True, project_dir=str(project))
    payload = json.loads(result)
    assert payload["status"] == "committed"
    assert payload["saved_count"] >= 1
    assert len(list((project / ".magnolia" / "staging").glob("*.md"))) >= 1, \
        "commit=True must save"


def test_commit_true_is_ungated(project, monkeypatch):
    """commit=True bypasses should_extract — even a session that would fail the
    gate still commits."""
    from compchem_memory import server, extraction
    monkeypatch.setattr(
        extraction.AutomaticMemoryExtractor, "should_extract",
        lambda self, path: False,
    )
    distill = _tool(server)

    result = distill(commit=True, project_dir=str(project))
    payload = json.loads(result)
    assert payload["status"] == "committed"
    assert payload["saved_count"] >= 1


@pytest.mark.parametrize("route_available", (True, False))
def test_manual_tool_event_commit_handles_exhausted_retry_by_route(
    project, monkeypatch, route_available,
):
    from compchem_memory import extraction, server

    session = next((project / ".magnolia" / "sessions").glob("*.jsonl"))
    state_path = project / "extraction-state.yaml"
    initial_state = {
        "last_cursor": "",
        "session_retries": {
            session.name: {
                "attempts": 5,
                "next_retry_at": None,
                "exhausted": True,
            },
        },
    }
    state_path.write_text(yaml.safe_dump(initial_state))
    llm_calls = []
    monkeypatch.setattr(extraction, "is_llm_available", lambda: route_available)
    monkeypatch.setattr(
        extraction.AutomaticMemoryExtractor,
        "_llm_distill",
        lambda _self, events: llm_calls.append(events) or [{
            "type": "success_pattern",
            "title": "LLM-only extraction",
            "content": "Generated through the configured LLM route.",
            "tags": ["llm"],
            "tools": [],
            "confidence": 0.9,
        }],
    )

    payload = json.loads(_tool(server)(commit=True, project_dir=str(project)))

    if route_available:
        assert payload["status"] == "committed"
        assert payload["saved_count"] == 1
        assert len(llm_calls) == 1
        assert "session_retries" not in yaml.safe_load(state_path.read_text())
        return

    assert payload["status"] == "skipped"
    assert payload["reason"] == "configured memory LLM is unavailable"
    assert payload["retry"] is False
    assert llm_calls == []
    assert yaml.safe_load(state_path.read_text()) == initial_state
    assert list((project / ".magnolia" / "staging").glob("*.md")) == []


def test_manual_tool_event_commit_respects_active_retry_backoff(
    project, monkeypatch,
):
    from compchem_memory import extraction, server

    session = next((project / ".magnolia" / "sessions").glob("*.jsonl"))
    scheduled_at = "2099-01-02T03:04:05+00:00"
    state_path = project / "extraction-state.yaml"
    initial_state = {
        "last_cursor": "",
        "session_retries": {
            session.name: {
                "attempts": 2,
                "next_retry_at": scheduled_at,
                "exhausted": False,
            },
        },
    }
    state_path.write_text(yaml.safe_dump(initial_state))
    provider_calls = []
    monkeypatch.setattr(extraction, "is_llm_available", lambda: True)
    monkeypatch.setattr(
        extraction.AutomaticMemoryExtractor,
        "_llm_distill",
        lambda _self, events: provider_calls.append(events) or [],
    )

    payload = json.loads(_tool(server)(commit=True, project_dir=str(project)))

    assert payload["status"] == "skipped"
    assert payload["reason"] == f"extraction retry scheduled at {scheduled_at}"
    assert payload["retry"] is False
    assert payload["next_retry_at"] == scheduled_at
    assert provider_calls == []
    assert yaml.safe_load(state_path.read_text()) == initial_state
