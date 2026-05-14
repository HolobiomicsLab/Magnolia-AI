"""Tests for the unified distillation engine: preview vs commit."""

import json
from pathlib import Path

import pytest

from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def _make_session(pd, events):
    p = pd / ".magnolia" / "sessions" / "2026-05-13_000000.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


_ERROR_FIX_EVENTS = [
    {"event_type": "session_start", "session_id": "s1", "project_id": "p", "schema_version": 2},
    {"event_type": "tool_error", "tool": "haddock3", "error": "missing topology file"},
    {"event_type": "tool_success", "tool": "haddock3", "result_summary": "ran ok after fix"},
]


def test_preview_returns_candidates_and_saves_nothing(project_dir, monkeypatch):
    from compchem_memory import extraction as ext
    monkeypatch.setattr(ext, "is_llm_available", lambda: False)

    session = _make_session(project_dir, _ERROR_FIX_EVENTS)
    engine = AutomaticMemoryExtractor(str(project_dir))

    candidates = engine.preview(session)
    assert isinstance(candidates, list)
    assert len(candidates) >= 1

    staging = project_dir / ".magnolia" / "staging"
    assert list(staging.glob("*.md")) == [], "preview must not write to staging"


def test_commit_saves_to_staging(project_dir, monkeypatch):
    from compchem_memory import extraction as ext
    monkeypatch.setattr(ext, "is_llm_available", lambda: False)

    session = _make_session(project_dir, _ERROR_FIX_EVENTS)
    engine = AutomaticMemoryExtractor(str(project_dir))

    saved = engine.commit(session, str(project_dir))
    assert isinstance(saved, list)
    assert len(saved) >= 1

    staging = project_dir / ".magnolia" / "staging"
    assert len(list(staging.glob("*.md"))) >= 1, "commit must write to staging"


def test_preview_and_commit_produce_same_candidate_set(project_dir, monkeypatch):
    from compchem_memory import extraction as ext
    monkeypatch.setattr(ext, "is_llm_available", lambda: False)

    session = _make_session(project_dir, _ERROR_FIX_EVENTS)
    engine = AutomaticMemoryExtractor(str(project_dir))

    preview_candidates = engine.preview(session)
    engine2 = AutomaticMemoryExtractor(str(project_dir))
    events = engine2._read_events(session)
    commit_candidates = engine2._generate_candidates(events)

    assert [c["title"] for c in preview_candidates] == [c["title"] for c in commit_candidates]
