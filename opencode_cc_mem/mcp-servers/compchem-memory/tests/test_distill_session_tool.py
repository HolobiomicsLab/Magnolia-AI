"""Tests for memory_distill_session: preview default, commit flag."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path, monkeypatch):
    from compchem_memory import server
    from compchem_memory.storage import ensure_project_store
    from compchem_memory.capture import get_session_manager, reset_registry

    reset_registry()
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
