"""Harness-neutral transcript rows retain evidence without retaining secrets."""

import re
from concurrent.futures import ThreadPoolExecutor

from compchem_memory.hook.events import AgentRef, HookEvent, ToolCall
from compchem_memory.hook.transcript import append_event, read_turns, transcript_path


def _event(kind: str, **overrides) -> HookEvent:
    values = {
        "harness": "opencode", "event": kind, "session_id": "ses-1",
        "cwd": "/project", "turn_id": "turn-1",
    }
    values.update(overrides)
    return HookEvent(**values)


def _tool(name: str = "read", output: str | None = "contents") -> ToolCall:
    return ToolCall(name=name, server=None, raw_name=name, call_id="call-1",
                    input={"path": "example.txt"}, output=output)


def test_transcript_rows_by_kind(tmp_path):
    events = [
        _event("session-start"),
        _event("prompt", prompt="Inspect this"),
        _event("pre-tool", tool=_tool()),
        _event("post-tool", tool=_tool(output="result"), turn_id="turn-2"),
        _event("turn-end", assistant_text="Done"),
        _event("turn-end", assistant_text=None),
        _event("agent-start", agent=AgentRef(id="agent-1", type="Explore")),
        _event("agent-end", agent=AgentRef(id="agent-1", type="Explore")),
        _event("session-end"),
    ]
    for event in events:
        append_event(tmp_path, event)

    path = transcript_path(tmp_path, "opencode", "ses-1")
    assert path == tmp_path / "transcripts" / "opencode" / "ses-1.jsonl"
    rows = read_turns(path)
    assert [row["kind"] for row in rows] == [
        "session-start", "prompt", "tool", "tool", "assistant",
        "agent-start", "agent-end", "session-end",
    ]
    assert rows[1]["text"] == "Inspect this"
    assert rows[2]["phase"] == "pre"
    assert rows[2]["input"] == {"path": "example.txt"}
    assert "output" not in rows[2] and "is_error" not in rows[2]
    assert rows[3]["output"] == "result" and rows[3]["is_error"] is False
    assert rows[4]["text"] == "Done"
    assert all(row["schema_version"] == 1 for row in rows)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", row["ts"]) for row in rows)


def test_transcript_truncates_and_escapes(tmp_path):
    append_event(tmp_path, _event("post-tool", tool=_tool(output="e\x00" + "x" * (5 * 1024 * 1024))))
    long_tool = ToolCall(name="write", server=None, raw_name="write", call_id=None,
                         input={"text": "é" * 4001})
    append_event(tmp_path, _event("pre-tool", tool=long_tool))

    rows = read_turns(transcript_path(tmp_path, "opencode", "ses-1"))
    assert rows[0]["output"] == "e" + "x" * 1998
    assert "\x00" not in rows[0]["output"]
    assert isinstance(rows[1]["input"], str) and len(rows[1]["input"]) == 4000
    assert rows[1]["truncated"] is True


def test_transcript_concurrent_appends(tmp_path):
    def append_batch(batch: int):
        for index in range(50):
            append_event(tmp_path, _event("prompt", prompt=f"{batch}/{index} " + "payload" * 200))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append_batch, range(8)))

    rows = read_turns(transcript_path(tmp_path, "opencode", "ses-1"))
    assert len(rows) == 400
    assert len({row["text"] for row in rows}) == 400


def test_transcript_scrubs_secrets(tmp_path):
    fabricated_key = "sk-test-00000000000000000000"
    bearer = "Bearer fabricated-token-0000"
    straddling = "x" * 1996 + bearer + "tail"
    secret_tool = ToolCall(name="shell", server=None, raw_name="shell", call_id="secret-input",
                           input={"db": {"password": "hunter2-plain"},
                                  "assignment": f"OPENAI_API_KEY={fabricated_key}"})
    append_event(tmp_path, _event("pre-tool", tool=secret_tool))
    append_event(tmp_path, _event("post-tool", tool=_tool(output=f"{bearer}\n{straddling}")))

    raw = transcript_path(tmp_path, "opencode", "ses-1").read_text(encoding="utf-8")
    for secret in ("hunter2-plain", fabricated_key, bearer, "fabricated-token-0000"):
        assert secret not in raw
    rows = read_turns(transcript_path(tmp_path, "opencode", "ses-1"))
    assert rows[0]["input"]["db"]["password"] == "[REDACTED_SECRET]"
    assert rows[0]["input"]["assignment"] == "OPENAI_API_KEY=[REDACTED_SECRET]"
    assert len(rows[1]["output"]) == 2000
    assert "[REDACTED_SECRET]" in rows[1]["output"]


def test_transcript_redacts_secret_keys_recursively(tmp_path):
    secrets = (
        "hunter2", "918273645", "fake-list-password", "it's-a-fake-pw",
        'fakepw"tailsecret', "fake-private-key", "tiny", "fake-passwd",
        "fake-cookie", "fake-api-key", "ZmFrZS1jcmVkZW50aWFscw==",
    )
    tool = ToolCall(name="shell", server=None, raw_name="shell", call_id="nested-secrets", input={
        "db": {
            "password": secrets[0],
            "nested": [
                {"password": 918273645},
                {"password": [secrets[2]]},
                {"password": secrets[3]},
                {"password": secrets[4]},
            ],
        },
        "PRIVATE_KEY": secrets[5],
        "pwd": secrets[6],
        "passwd": secrets[7],
        "COOKIE": secrets[8],
        "api-key": secrets[9],
        "ordinary": "kept",
        "text": f"Authorization: Basic {secrets[10]}",
    })
    append_event(tmp_path, _event("pre-tool", tool=tool))

    raw = transcript_path(tmp_path, "opencode", "ses-1").read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in raw
    row = read_turns(transcript_path(tmp_path, "opencode", "ses-1"))[0]
    assert row["input"]["db"]["password"] == "[REDACTED_SECRET]"
    assert all(item["password"] == "[REDACTED_SECRET]" for item in row["input"]["db"]["nested"])
    assert all(row["input"][key] == "[REDACTED_SECRET]" for key in (
        "PRIVATE_KEY", "pwd", "passwd", "COOKIE", "api-key",
    ))
    assert row["input"]["ordinary"] == "kept"
    assert row["input"]["text"] == "Authorization: Basic [REDACTED_SECRET]"


def test_transcript_round_trips_unicode_line_separators(tmp_path):
    prompt = "prompt\u2028one\u2029two\u0085three"
    output = "output\u2028one\u2029two\u0085three"
    append_event(tmp_path, _event("prompt", prompt=prompt))
    append_event(tmp_path, _event("post-tool", tool=_tool(output=output)))

    rows = read_turns(transcript_path(tmp_path, "opencode", "ses-1"))
    assert len(rows) == 2
    assert rows[0]["text"] == prompt
    assert rows[1]["output"] == output


def test_transcript_keeps_evidence_tool_outputs_whole(tmp_path):
    output = "evidence" * 1250
    append_event(tmp_path, _event("post-tool", tool=_tool(name="memory_search", output=output)))
    row = read_turns(transcript_path(tmp_path, "opencode", "ses-1"))[0]
    assert row["output"] == output


def test_transcript_phase_and_agent_on_every_row(tmp_path):
    agent = AgentRef(id="agent-1", type=None)
    for event in (
        _event("session-start", turn_id=None),
        _event("agent-start", agent=agent),
        _event("pre-tool", agent=agent, tool=_tool()),
        _event("post-tool", agent=agent, tool=_tool()),
        _event("turn-end", agent=agent, assistant_text="finished"),
    ):
        append_event(tmp_path, event)

    rows = read_turns(transcript_path(tmp_path, "opencode", "ses-1"))
    assert rows[0]["agent"] is None and rows[0]["turn_id"] is None
    assert all(row["agent"] == {"id": "agent-1", "type": None} for row in rows[1:])
    assert [row["phase"] for row in rows if row["kind"] == "tool"] == ["pre", "post"]
    assert all(set(row) >= {"schema_version", "ts", "kind", "turn_id", "agent"} for row in rows)


def test_read_turns_skips_malformed_lines(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text('{"kind":"prompt"}\nnot json\n[]\n{"kind":"tool"}\n', encoding="utf-8")
    assert read_turns(path) == [{"kind": "prompt"}, {"kind": "tool"}]


def test_transcript_dot_dot_session_id_stays_in_store(tmp_path):
    store = tmp_path / "workspace" / "projects" / "alpha" / ".magnolia"
    append_event(store, _event("prompt", prompt="hi", harness="../json", session_id="../../../../escape"))
    append_event(store, _event("prompt", prompt="hi", session_id="a/b"))

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) == 2
    assert all((store / "transcripts") in p.parents for p in written)
    assert transcript_path(store, "opencode", "a/b").parent == store / "transcripts" / "opencode"
    assert transcript_path(store, "opencode", "a/b") != transcript_path(store, "opencode", "a_b")
