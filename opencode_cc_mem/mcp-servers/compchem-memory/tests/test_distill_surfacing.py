"""Tests: distillation surfaces quotes to distill.log and the notices queue,
and the @captured decorator drains notices into tool responses."""

import json
from pathlib import Path

import pytest

from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.storage import ensure_project_store
from compchem_memory import distill_log
from compchem_memory.capture import captured, get_session_manager, reset_registry


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    from compchem_memory import extraction as ext
    monkeypatch.setattr(ext, "is_llm_available", lambda: False)
    return pd


_ERROR_FIX = [
    {"event_type": "session_start", "session_id": "s1", "project_id": "p", "schema_version": 2},
    {"event_type": "tool_error", "tool": "haddock3", "error": "missing topology"},
    {"event_type": "tool_success", "tool": "haddock3", "result_summary": "ok"},
]


def _make_session(pd):
    p = pd / ".magnolia" / "sessions" / "2026-05-13_000000.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in _ERROR_FIX) + "\n")
    return p


def test_commit_writes_distill_log(project_dir):
    session = _make_session(project_dir)
    engine = AutomaticMemoryExtractor(str(project_dir))
    engine.commit(session, str(project_dir))

    log = project_dir / ".magnolia" / "distill.log"
    assert log.exists()
    assert len(log.read_text().strip()) > 0


def test_commit_pushes_a_notice(project_dir):
    session = _make_session(project_dir)
    engine = AutomaticMemoryExtractor(str(project_dir))
    engine.commit(session, str(project_dir))

    notices = distill_log.drain_distill_notices(str(project_dir))
    assert len(notices) >= 1


def test_commit_with_no_candidates_does_not_push_noise(project_dir, monkeypatch):
    """A commit that saves nothing should not spam the log / notices."""
    session = _make_session(project_dir)
    engine = AutomaticMemoryExtractor(str(project_dir))
    monkeypatch.setattr(engine, "_generate_candidates", lambda events: [])
    engine.commit(session, str(project_dir))

    assert distill_log.drain_distill_notices(str(project_dir)) == []
    log = project_dir / ".magnolia" / "distill.log"
    assert not log.exists() or log.read_text().strip() == ""


def test_decorator_drains_notices_into_response(project_dir):
    # Pre-seed a notice as if a background distillation had pushed it
    distill_log.push_distill_notice(str(project_dir), "QUOTE-X", "1 learning distilled")

    @captured(source="compchem-memory")
    def some_tool(project_dir: str | None = None) -> str:
        return json.dumps({"status": "ok"})

    result = some_tool(project_dir=str(project_dir))
    assert "QUOTE-X" in result
    assert "1 learning distilled" in result
    # queue is drained
    assert distill_log.drain_distill_notices(str(project_dir)) == []
