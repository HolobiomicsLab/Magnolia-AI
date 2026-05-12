"""Tests for the @captured decorator: emits tool_call + tool_success/tool_error."""

import json
import tempfile
from pathlib import Path

import pytest

from compchem_memory.capture import captured, get_session_manager, reset_registry


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "sessions").mkdir(parents=True)
    return pd


def _read_events(project_dir):
    sessions = project_dir / ".magnolia" / "sessions"
    files = list(sessions.glob("*.jsonl"))
    assert len(files) == 1
    return [json.loads(l) for l in files[0].read_text().splitlines()]


def test_decorator_emits_tool_call_and_success(project_dir):
    @captured(source="compchem-memory")
    def memory_dummy(arg1: str, project_dir: str | None = None) -> str:
        return "ok:" + arg1

    result = memory_dummy("hello", project_dir=str(project_dir))
    assert result == "ok:hello"

    events = _read_events(project_dir)
    assert len(events) == 3  # header + tool_call + tool_success
    assert events[0]["event_type"] == "session_start"
    assert events[1]["event_type"] == "tool_call"
    assert events[1]["source"] == "compchem-memory"
    assert events[1]["tool"] == "memory_dummy"
    assert events[2]["event_type"] == "tool_success"
    assert events[2]["tool"] == "memory_dummy"
    assert "duration_ms" in events[2]


def test_decorator_emits_tool_error_and_reraises(project_dir):
    @captured(source="compchem-tools")
    def failing_tool(project_dir: str | None = None) -> str:
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError, match="synthetic failure"):
        failing_tool(project_dir=str(project_dir))

    events = _read_events(project_dir)
    assert len(events) == 3
    assert events[2]["event_type"] == "tool_error"
    assert events[2]["tool"] == "failing_tool"
    assert "ValueError" in events[2]["error"]
    assert "synthetic failure" in events[2]["error"]


def test_decorator_logging_failure_does_not_break_tool(project_dir, monkeypatch):
    """If the SessionManager raises, the wrapped tool still runs and returns."""
    from compchem_memory import capture as cap_mod

    @captured(source="compchem-memory")
    def memory_dummy(project_dir: str | None = None) -> str:
        return "ok"

    mgr = cap_mod.get_session_manager(str(project_dir))
    def broken_record(*args, **kwargs):
        raise RuntimeError("logging died")
    monkeypatch.setattr(mgr, "record", broken_record)

    result = memory_dummy(project_dir=str(project_dir))
    assert result == "ok"
