"""Recover assistant reply blocks from the three supported client journals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compchem_memory.hook.client_transcripts import assistant_texts


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "client-transcripts"
CLAUDE_TURN = "4bb02dbd-650d-4e13-a0bc-9511d9c860cb"
CLAUDE_AGENT = "a18415b0acfbb69d4"
CODEX_TURN = "01a1df67-264d-7472-b477-66828e7d31b6"
VIBE_TURN = "turn-2c40443c7526b229e2076d661938b73f"


def test_claude_reply_blocks_and_subagent_attribution():
    main = FIXTURES / "claude" / "session.jsonl"
    subagent = FIXTURES / "claude" / "subagents" / f"agent-{CLAUDE_AGENT}.jsonl"

    assert assistant_texts("claude", str(main), turn_id=CLAUDE_TURN, agent_id=None) == [
        "CHECKING.",
        'note.txt says: "alpha line"\n\n'
        "Next I'll hand note2.txt to a subagent and wait for its answer.",
        'The subagent\'s report says note2.txt contains: "beta line"\n\n'
        'To summarise: note.txt contains "alpha line", and note2.txt contains "beta line".',
    ]
    assert assistant_texts(
        "claude", str(subagent), turn_id=CLAUDE_TURN, agent_id=CLAUDE_AGENT,
    ) == ["beta line"]
    assert assistant_texts(
        "claude", str(subagent), turn_id=CLAUDE_TURN, agent_id="another-agent",
    ) == []


def test_codex_reply_blocks_follow_task_boundaries():
    rollout = FIXTURES / "codex" / "rollout.jsonl"

    assert assistant_texts("codex", str(rollout), turn_id=CODEX_TURN, agent_id=None) == [
        "CHECKING.",
        "```sh\ncat note.txt\n```\n\n> “alpha line”\n\n"
        "The note contains a single line: “alpha line.”",
    ]
    assert assistant_texts("codex", str(rollout), turn_id="another-turn", agent_id=None) == []


def test_vibe_reply_blocks_exclude_reasoning_entries():
    session = FIXTURES / "vibe" / "session"

    assert assistant_texts("vibe", str(session), turn_id=VIBE_TURN, agent_id=None) == [
        "CHECKING.",
        'note.txt contains exactly one line: "alpha line". '
        "It is a minimal placeholder note with no other content or instructions.",
    ]


@pytest.mark.parametrize("harness", ["claude", "codex", "vibe", "unknown"])
def test_unreadable_garbage_and_missing_transcripts_return_empty(tmp_path, harness):
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("partial line\n{not json}\n[]\n", encoding="utf-8")
    transcript_ref = str(tmp_path) if harness == "vibe" else str(garbage)

    assert assistant_texts(
        harness, transcript_ref, turn_id="turn", agent_id=None, max_bytes=8,
    ) == []
    assert assistant_texts(
        harness, str(tmp_path / "missing"), turn_id="turn", agent_id=None,
    ) == []


def test_tail_reader_drops_a_partial_first_line_and_scrubs_secrets(tmp_path):
    turn_id = "tail-turn"
    path = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "ignore old block"}],
        }},
        {"type": "turn_context", "payload": {"turn_id": turn_id}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": "OPENAI_API_KEY=sk-test-00000000000000000000",
            }],
        }},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    last_three_bytes = "".join(json.dumps(row) + "\n" for row in rows[-3:]).encode()

    assert assistant_texts(
        "codex", str(path), turn_id=turn_id, agent_id=None,
        max_bytes=len(last_three_bytes) + 5,
    ) == ["OPENAI_API_KEY=[REDACTED_SECRET]"]


def test_tail_reader_keeps_complete_first_line_on_exact_boundary(tmp_path):
    turn_id = "boundary-turn"
    path = tmp_path / "session.jsonl"
    prefix = json.dumps({"type": "ignored"}) + "\n"
    tail_rows = [
        {"type": "user", "promptId": turn_id},
        {"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "complete first line"}],
        }},
    ]
    tail = "".join(json.dumps(row) + "\n" for row in tail_rows)
    path.write_text(prefix + tail, encoding="utf-8")

    assert assistant_texts(
        "claude", str(path), turn_id=turn_id, agent_id=None,
        max_bytes=len(tail.encode("utf-8")) + 1,
    ) == ["complete first line"]


def test_tail_reader_never_reads_more_than_budget_for_one_huge_line(tmp_path, monkeypatch):
    path = tmp_path / "huge.jsonl"
    path.write_bytes(b"x" * (5 * 1024 * 1024))
    max_bytes = 1024
    observed = {"bytes": 0, "unbounded": False}
    original_open = Path.open

    class TrackedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def seek(self, *args):
            return self.stream.seek(*args)

        def read(self, size=-1):
            if size < 0:
                observed["unbounded"] = True
            data = self.stream.read(size)
            observed["bytes"] += len(data)
            return data

        def readline(self, size=-1):
            if size < 0:
                observed["unbounded"] = True
            data = self.stream.readline(size)
            observed["bytes"] += len(data)
            return data

    def tracked_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)
        return TrackedReader(stream) if candidate == path else stream

    monkeypatch.setattr(Path, "open", tracked_open)

    assert assistant_texts(
        "codex", str(path), turn_id="turn", agent_id=None, max_bytes=max_bytes,
    ) == []
    assert observed == {"bytes": max_bytes, "unbounded": False}


def test_each_client_keeps_two_turns_isolated(tmp_path):
    claude = tmp_path / "claude.jsonl"
    claude_rows = [
        {"type": "user", "promptId": "claude-1"},
        {"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "claude one"}],
        }},
        {"type": "user", "promptId": "claude-2"},
        {"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "claude two"}],
        }},
    ]
    claude.write_text("".join(json.dumps(row) + "\n" for row in claude_rows), encoding="utf-8")

    codex = tmp_path / "codex.jsonl"
    codex_rows = []
    for turn_id, text in (("codex-1", "codex one"), ("codex-2", "codex two")):
        codex_rows.extend([
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
        ])
    codex.write_text("".join(json.dumps(row) + "\n" for row in codex_rows), encoding="utf-8")

    vibe = tmp_path / "vibe"
    journal = vibe / "journal"
    journal.mkdir(parents=True)
    vibe_rows = [
        {"payload": {"delta": [{"entry": {
            "id": "reasoning", "turnId": "vibe-1", "content": [{"text": "private reasoning"}],
        }}]}},
        {"payload": {"delta": [{"entry": {
            "id": "answer-1", "role": "assistant", "turnId": "vibe-1",
            "content": [{"type": "text", "text": "vibe one"}],
        }}]}},
        {"payload": {"delta": [{"entry": {
            "id": "answer-2", "role": "assistant", "turnId": "vibe-2",
            "content": [{"type": "text", "text": "vibe two"}],
        }}]}},
    ]
    (journal / "001.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in vibe_rows), encoding="utf-8",
    )

    for harness, transcript_ref, first_turn, first_text, second_turn, second_text in (
        ("claude", claude, "claude-1", "claude one", "claude-2", "claude two"),
        ("codex", codex, "codex-1", "codex one", "codex-2", "codex two"),
        ("vibe", vibe, "vibe-1", "vibe one", "vibe-2", "vibe two"),
    ):
        assert assistant_texts(
            harness, str(transcript_ref), turn_id=first_turn, agent_id=None,
        ) == [first_text]
        assert assistant_texts(
            harness, str(transcript_ref), turn_id=second_turn, agent_id=None,
        ) == [second_text]


def test_none_turn_id_does_not_guess_across_turns():
    assert assistant_texts(
        "codex", str(FIXTURES / "codex" / "rollout.jsonl"), turn_id=None, agent_id=None,
    ) == []
