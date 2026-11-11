"""Session id must be bound before boot work, and both layers must share it.

Regression for 2026-11-10: .current-session-id was written only after the
70-170 s boot pipeline, so the first shell calls inherited the previous
session's id and the live file looked closed to startup_scan.
"""

import json
from datetime import datetime, timezone

from compchem_memory.tiers.session import SessionManager, init_session_identity


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_init_session_identity_writes_marker_and_header(tmp_path):
    sid = init_session_identity(tmp_path)

    assert sid is not None
    assert sid.startswith(_today() + "_")

    marker = tmp_path / ".magnolia" / ".current-session-id"
    assert marker.read_text().strip() == sid

    session_file = tmp_path / ".magnolia" / "sessions" / f"{sid}.jsonl"
    assert session_file.exists()
    header = json.loads(session_file.read_text().splitlines()[0])
    assert header["event_type"] == "session_start"
    assert header["session_id"] == sid
    assert header["project_id"] == tmp_path.name
    assert header["schema_version"] == 2


def test_session_manager_adopts_marker_id(tmp_path):
    sid = init_session_identity(tmp_path)
    sessions_dir = tmp_path / ".magnolia" / "sessions"

    mgr = SessionManager(sessions_dir, project_id=tmp_path.name, project_dir=str(tmp_path))
    mgr.record("tool_call", {"tool_name": "shell"})

    expected = sessions_dir / f"{sid}.jsonl"
    assert expected.exists()
    rows = [json.loads(line) for line in expected.read_text().splitlines()]
    assert [row["event_type"] for row in rows] == ["session_start", "tool_call"]
    assert all(row["session_id"] == sid for row in rows)


def test_session_manager_falls_back_to_newest_today_file(tmp_path):
    sessions_dir = tmp_path / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True)
    sid = _today() + "_120000"
    (sessions_dir / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "event_type": "session_start",
                "session_id": sid,
                "project_id": tmp_path.name,
                "project_dir": str(tmp_path),
                "schema_version": 2,
            }
        )
        + "\n"
    )

    mgr = SessionManager(sessions_dir, project_id=tmp_path.name, project_dir=str(tmp_path))
    assert mgr._current_session_id == sid


def test_stale_marker_from_yesterday_is_ignored(tmp_path):
    sessions_dir = tmp_path / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True)
    marker = tmp_path / ".magnolia" / ".current-session-id"
    marker.write_text("1999-01-01_000000")

    mgr = SessionManager(sessions_dir, project_id=tmp_path.name, project_dir=str(tmp_path))
    assert mgr._current_session_id is None
