"""Session mappings from the neutral hook and the legacy OpenCode plugin."""

import json

from compchem_memory.hook.sessions import iter_sessions, record_session


def test_record_session_once(tmp_path):
    assert record_session(tmp_path, "claude-code", "s1", "/project", "transcript.jsonl") is True
    assert record_session(tmp_path, "claude-code", "s1", "/other", "other.jsonl") is False

    rows = [json.loads(line) for line in (tmp_path / "agent-sessions.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["harness"] == "claude-code" and rows[0]["session_id"] == "s1"
    assert rows[0]["directory"] == "/project" and rows[0]["transcript_ref"] == "transcript.jsonl"
    assert "ts" in rows[0]


def test_iter_sessions_merges_legacy(tmp_path):
    record_session(tmp_path, "claude-code", "s1", "/project", "transcript.jsonl")
    (tmp_path / "opencode-sessions.jsonl").write_text(json.dumps({
        "ts": "2026-11-15T10:00:00.000Z", "opencode_session_id": "ses-legacy",
        "directory": "/legacy", "worktree": "main",
    }) + "\ninvalid\n", encoding="utf-8")

    rows = list(iter_sessions(tmp_path))
    assert len(rows) == 2
    assert rows[0]["session_id"] == "s1"
    assert rows[1] == {"ts": "2026-11-15T10:00:00.000Z", "harness": "opencode",
                       "session_id": "ses-legacy", "directory": "/legacy", "worktree": "main",
                       "transcript_ref": None}
