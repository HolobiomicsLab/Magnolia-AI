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


def test_iter_sessions_round_trips_unicode_line_separators(tmp_path):
    row = {
        "ts": "2026-11-15T10:00:00.000Z", "harness": "claude-code", "session_id": "s1",
        "directory": "/project\u2028one\u2029two\u0085three", "transcript_ref": None,
    }
    (tmp_path / "agent-sessions.jsonl").write_text(
        "\n" + json.dumps(row, ensure_ascii=False) + "\nmalformed\n", encoding="utf-8",
    )

    assert list(iter_sessions(tmp_path)) == [row]


def test_record_session_dot_dot_id_stays_in_store(tmp_path):
    store = tmp_path / "store"
    assert record_session(store, "../json", "../../../../escape", "/project", None) is True
    assert record_session(store, "../json", "../../../../escape", "/project", None) is False

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all(store in p.parents for p in written)
    [row] = [json.loads(line) for line in (store / "agent-sessions.jsonl").read_text().splitlines()]
    assert row["session_id"] == "../../../../escape"
