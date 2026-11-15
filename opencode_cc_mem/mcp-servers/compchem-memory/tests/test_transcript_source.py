"""One transcript/export contract across legacy and hook-fed clients."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from compchem_memory.hook.state import safe_component
from compchem_memory.opencode_ingest import reconstruct_transcript
from compchem_memory.transcript_source import (
    SessionRef,
    active_opencode_sid,
    exporter_for,
    list_sessions,
    magnolia_exporter,
    resolve_transcript,
)


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_legacy_only_order_and_active_unchanged(tmp_path):
    mapping = tmp_path / "opencode-sessions.jsonl"
    _write_jsonl(mapping, [
        {"ts": "2026-11-15T09:00:00Z", "opencode_session_id": "ses-a"},
        {"ts": "2026-11-15T10:00:00Z", "opencode_session_id": "ses-b"},
        {"ts": "2026-11-15T11:00:00Z", "opencode_session_id": "ses-a"},
    ])

    refs = list_sessions(tmp_path)

    assert [ref.session_id for ref in refs] == ["ses-a", "ses-b"]
    assert refs == [
        SessionRef("opencode", "ses-a", "ses-a", "2026-11-15T09:00:00Z",
                   "2026-11-15T11:00:00Z", True),
        SessionRef("opencode", "ses-b", "ses-b", "2026-11-15T10:00:00Z",
                   "2026-11-15T10:00:00Z", True),
    ]
    assert active_opencode_sid(tmp_path) == "ses-a"


def test_mixed_store_lists_both(tmp_path):
    _write_jsonl(tmp_path / "opencode-sessions.jsonl", [
        {"ts": "2026-11-15T10:00:00Z", "opencode_session_id": "ses-legacy"},
    ])
    _write_jsonl(tmp_path / "agent-sessions.jsonl", [
        {"ts": "2026-11-15T12:00:00Z", "harness": "claude",
         "session_id": "claude-session", "transcript_ref": None},
        {"ts": "2026-11-15T08:00:00Z", "harness": "codex",
         "session_id": "thread /β", "transcript_ref": None},
        {"ts": "2026-11-15T13:00:00Z", "harness": "opencode",
         "session_id": "ses-legacy", "transcript_ref": None},
    ])
    claude_path = tmp_path / "transcripts" / "claude" / "claude-session.jsonl"
    _write_jsonl(claude_path, [{"kind": "prompt", "text": "hello"}])
    os.utime(claude_path, (1_700_000_000, 1_700_000_000))

    refs = list_sessions(tmp_path)

    assert [(ref.harness, ref.session_id) for ref in refs] == [
        ("opencode", "ses-legacy"),
        ("codex", "thread /β"),
        ("claude", "claude-session"),
    ]
    assert refs[1].key == f"codex-{safe_component('thread /β')}"
    assert refs[1].last_ts is None
    assert refs[1].sealable is False
    assert refs[2].key == "claude-claude-session"
    assert refs[2].last_ts == datetime.fromtimestamp(
        1_700_000_000, timezone.utc,
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert sum(ref.session_id == "ses-legacy" for ref in refs) == 1


def test_magnolia_exporter_reconstructs_like_opencode(tmp_path):
    ref = SessionRef("claude", "session-1", "claude-session-1", None, None, False)
    _write_jsonl(tmp_path / "transcripts" / "claude" / "session-1.jsonl", [
        {"kind": "session-start", "agent": None},
        {"kind": "prompt", "text": "inspect sample", "agent": None},
        {"kind": "assistant", "text": "I will inspect it", "agent": {"id": "a1"}},
        {"kind": "tool", "phase": "pre", "name": "search", "agent": {"id": "a1"}},
        {"kind": "tool", "phase": "post", "name": "search", "server": "memory",
         "input": {"query": "sample"}, "output": "one match", "is_error": False,
         "agent": {"id": "a1"}},
        {"kind": "tool", "phase": "post", "name": "read", "server": None,
         "input": {"path": "missing"}, "output": "not found", "is_error": True,
         "agent": None},
        {"kind": "injected", "channel": "action_memory", "entry_ids": ["entry-7"],
         "text": "known recovery", "agent": None},
    ])

    exported = magnolia_exporter(tmp_path)(ref)
    assert exported is not None
    assert [message["info"]["id"] for message in exported["messages"]] == [
        "claude-session-1:1", "claude-session-1:2", "claude-session-1:4",
        "claude-session-1:5",
    ]
    assert exported["messages"][1]["info"]["agent"] == {"id": "a1"}
    first_tool = exported["messages"][2]["parts"][0]
    assert first_tool["name"] == "search"
    assert first_tool["server"] == "memory"
    assert first_tool["state"] == {
        "status": "completed", "input": {"query": "sample"}, "output": "one match",
    }
    assert exported["messages"][3]["parts"][0]["state"]["status"] == "error"
    assert exported["known_entry_ids"] == ["entry-7"]

    rendered = reconstruct_transcript(exported, tool_chars=1000)
    assert rendered.split("\n\n") == [
        "SYSTEM: Magnolia already supplied memory entries entry-7; do not re-stage them.",
        "USER: inspect sample",
        "ASSISTANT: I will inspect it",
        "ASSISTANT (tool:search): one match",
        "ASSISTANT (tool:read): not found",
    ]


def test_injected_string_exporter_still_used_for_opencode(tmp_path):
    calls = []

    def injected(session_id):
        calls.append(session_id)
        return {"messages": []}

    export = exporter_for(tmp_path, injected=injected)
    ref = SessionRef("opencode", "ses-raw", "ses-raw", None, None, True)

    assert export(ref) == {"messages": []}
    assert calls == ["ses-raw"]


def test_resolve_transcript_both_kinds(tmp_path):
    legacy = tmp_path / "opencode-transcripts" / "ses-old.json"
    legacy.parent.mkdir()
    legacy.write_text("[]", encoding="utf-8")
    fallback = tmp_path / "sessions" / "ses-old.jsonl"
    _write_jsonl(fallback, [{"event_type": "prompt"}])

    session_id = "thread /one"
    neutral = tmp_path / "transcripts" / "codex" / f"{safe_component(session_id)}.jsonl"
    _write_jsonl(neutral, [{"kind": "prompt", "text": "hello"}])
    _write_jsonl(tmp_path / "agent-sessions.jsonl", [{
        "ts": "2026-11-15T08:00:00Z", "harness": "codex", "session_id": session_id,
    }])

    assert resolve_transcript(tmp_path, "ses-old") == legacy
    assert resolve_transcript(tmp_path, f"codex-{safe_component(session_id)}") == neutral
    assert resolve_transcript(tmp_path, "missing") is None


def test_collegium_materialised_transcript_regression(tmp_path):
    # Shape copied from collegium@e64e411 src/collegium/worker.py:105-129.
    store = tmp_path / ".magnolia"
    session_id = "exc_0123456789abcdef"
    transcript = store / "opencode-transcripts" / f"{session_id}.json"
    transcript.parent.mkdir(parents=True)
    message = {
        "info": {"role": "user"},
        "parts": [{"type": "text", "text": "the anchored excerpt"}],
    }
    transcript.write_text(json.dumps([message]), encoding="utf-8")

    assert resolve_transcript(store, session_id) == transcript


def _hook_session(store, session_id="claude-1"):
    """Record a real hook-fed session through the transcript writer."""
    from compchem_memory.hook.events import HookEvent, ToolCall
    from compchem_memory.hook.transcript import append_event

    base = dict(harness="claude", session_id=session_id, cwd=str(store), turn_id="t1")
    append_event(store, HookEvent(event="prompt", prompt="run it", **base))
    append_event(store, HookEvent(event="post-tool", tool=ToolCall(
        name="run_shell", server="compchem-tools", raw_name="mcp__compchem-tools__run_shell",
        call_id="c1", input={"cmd": "ls"}, output="a.txt"), **base))
    append_event(store, HookEvent(event="turn-end", assistant_text="Listed one file.", **base))
    _write_jsonl(store / "agent-sessions.jsonl", [
        {"ts": "2026-11-15T12:00:00.000Z", "harness": "claude",
         "session_id": session_id, "transcript_ref": None},
    ])


def test_real_tool_rows_keep_their_server(tmp_path):
    _hook_session(tmp_path)
    ref = [r for r in list_sessions(tmp_path) if r.harness == "claude"][0]

    export = magnolia_exporter(tmp_path)(ref)

    tool_parts = [part for message in export["messages"] for part in message["parts"]
                  if part.get("type") == "tool"]
    assert tool_parts and all(part.get("server") == "compchem-tools" for part in tool_parts)


def test_hook_sessions_route_to_the_magnolia_exporter(tmp_path):
    _hook_session(tmp_path)
    called = []
    exporter = exporter_for(tmp_path, injected=lambda sid: called.append(sid) or None)
    ref = [r for r in list_sessions(tmp_path) if r.harness == "claude"][0]

    export = exporter(ref)

    assert called == []
    assert export["messages"][0]["info"]["id"].startswith(f"{ref.key}:")


def test_cursor_round_trip_returns_only_new_rows(tmp_path):
    from compchem_memory.hook.events import HookEvent
    from compchem_memory.hook.transcript import append_event
    from compchem_memory.opencode_ingest import _last_message_id, _messages_after_cursor

    _hook_session(tmp_path)
    ref = [r for r in list_sessions(tmp_path) if r.harness == "claude"][0]
    exporter = magnolia_exporter(tmp_path)
    cursor = _last_message_id(exporter(ref)["messages"])
    append_event(tmp_path, HookEvent(harness="claude", session_id="claude-1", cwd=str(tmp_path),
                                     event="prompt", prompt="next", turn_id="t2"))

    new = _messages_after_cursor(exporter(ref)["messages"], cursor)

    assert len(new) == 1 and new[0]["info"]["role"] == "user"


def test_legacy_order_is_file_order_even_with_out_of_order_timestamps(tmp_path):
    _write_jsonl(tmp_path / "opencode-sessions.jsonl", [
        {"ts": "2026-11-15T11:00:00Z", "opencode_session_id": "ses-late"},
        {"ts": "2026-11-15T09:00:00Z", "opencode_session_id": "ses-early"},
    ])

    assert [ref.session_id for ref in list_sessions(tmp_path)] == ["ses-late", "ses-early"]


def test_hook_last_ts_keeps_milliseconds(tmp_path):
    _hook_session(tmp_path)
    path = tmp_path / "transcripts" / "claude" / "claude-1.jsonl"
    os.utime(path, (1_700_000_000, 1_700_000_000))

    ref = [r for r in list_sessions(tmp_path) if r.harness == "claude"][0]

    assert ref.last_ts == "2023-11-14T22:13:20.000Z"


def test_legacy_activity_uses_materialised_transcript_mtime(tmp_path):
    _write_jsonl(tmp_path / "opencode-sessions.jsonl", [{
        "ts": "2026-11-15T09:00:00Z",
        "opencode_session_id": "ses-resumed",
    }])
    transcript = tmp_path / "opencode-transcripts" / "ses-resumed.json"
    transcript.parent.mkdir()
    transcript.write_text('{"messages": []}', encoding="utf-8")
    activity = datetime(2026, 11, 15, 10, 30, tzinfo=timezone.utc).timestamp()
    os.utime(transcript, (activity, activity))

    ref = list_sessions(tmp_path)[0]

    assert ref.first_ts == "2026-11-15T09:00:00Z"
    assert ref.last_ts == "2026-11-15T10:30:00.000Z"
