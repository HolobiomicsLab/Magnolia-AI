"""Tests for SessionManager v2: project_id, header, restart recovery."""

import json
import tempfile
from pathlib import Path

import pytest

from compchem_memory.capture import get_session_manager, reset_registry
from compchem_memory.tiers.session import SessionManager


@pytest.fixture
def sessions_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / ".magnolia" / "sessions"


def test_first_record_writes_session_start_header(sessions_dir):
    mgr = SessionManager(sessions_dir, project_id="my_project", project_dir="/abs/path")
    mgr.record("tool_call", {"tool": "haddock3", "args_summary": "config=docking.cfg"})

    path = Path(mgr.get_session_log_path())
    lines = path.read_text().splitlines()
    assert len(lines) == 2

    header = json.loads(lines[0])
    assert header["event_type"] == "session_start"
    assert header["project_id"] == "my_project"
    assert header["project_dir"] == "/abs/path"
    assert header["schema_version"] == 2
    assert "session_id" in header
    assert "timestamp" in header


def test_every_event_carries_project_id_and_session_id(sessions_dir):
    mgr = SessionManager(sessions_dir, project_id="my_project", project_dir="/abs/path")
    mgr.record("tool_call", {"tool": "x"})
    mgr.record("tool_success", {"tool": "x"})

    path = Path(mgr.get_session_log_path())
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for event in lines[1:]:
        assert event["project_id"] == "my_project"
        assert event["session_id"] == lines[0]["session_id"]


def test_dict_keyed_registry_isolates_projects(tmp_path):
    reset_registry()
    pa = tmp_path / "proj_a"
    pb = tmp_path / "proj_b"
    (pa / ".magnolia" / "sessions").mkdir(parents=True)
    (pb / ".magnolia" / "sessions").mkdir(parents=True)

    mgr_a = get_session_manager(str(pa), project_id="proj_a")
    mgr_b = get_session_manager(str(pb), project_id="proj_b")
    assert mgr_a is not mgr_b

    mgr_a.record("tool_call", {"tool": "x"})
    mgr_b.record("tool_call", {"tool": "y"})

    mgr_a2 = get_session_manager(str(pa), project_id="proj_a")
    mgr_b2 = get_session_manager(str(pb), project_id="proj_b")
    assert mgr_a2 is mgr_a
    assert mgr_b2 is mgr_b


def test_restart_recovers_todays_session(sessions_dir):
    """When a SessionManager is created and a session file for today exists for this project,
    the new manager adopts it instead of starting a fresh file."""
    mgr1 = SessionManager(sessions_dir, project_id="proj_a", project_dir="/p/a")
    mgr1.record("tool_call", {"tool": "x"})
    first_path = mgr1.get_session_log_path()

    mgr2 = SessionManager(sessions_dir, project_id="proj_a", project_dir="/p/a")
    mgr2.record("tool_call", {"tool": "y"})

    assert mgr2.get_session_log_path() == first_path
    lines = Path(first_path).read_text().splitlines()
    assert len(lines) == 3


def test_read_methods_do_not_create_session_file(tmp_path):
    """get_recent / search / count_events_since on a never-recorded manager
    must NOT materialize a session file."""
    sessions_dir = tmp_path / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True)

    mgr = SessionManager(sessions_dir, project_id="p", project_dir=str(tmp_path))

    assert mgr.get_recent() == []
    assert mgr.search("anything") == []
    assert mgr.count_events_since("") == (0, 0)

    # No session files should have been created
    assert list(sessions_dir.glob("*.jsonl")) == []


def test_discover_does_not_adopt_wrong_project(tmp_path):
    """A SessionManager for project A must not adopt a session file written by project B,
    even if both share the same sessions_dir (defensive: real deployments don't do this,
    but the registry key must not be the only thing protecting against cross-adoption)."""
    import time

    sessions_dir = tmp_path / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True)

    mgr_a = SessionManager(sessions_dir, project_id="proj_a", project_dir="/a")
    mgr_a.record("tool_call", {"tool": "x"})
    file_a = mgr_a.get_session_log_path()

    # Wait a full second so the timestamp differs and filenames cannot collide.
    time.sleep(1.1)

    # Brand-new SessionManager for a DIFFERENT project, same sessions_dir.
    mgr_b = SessionManager(sessions_dir, project_id="proj_b", project_dir="/b")
    mgr_b.record("tool_call", {"tool": "y"})
    file_b = mgr_b.get_session_log_path()

    assert file_a != file_b
