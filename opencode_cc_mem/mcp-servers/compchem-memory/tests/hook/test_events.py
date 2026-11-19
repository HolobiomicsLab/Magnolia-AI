"""Tests for the harness-neutral hook event model.

The brief's tests are kept verbatim at the top of each section; the extra
cases pin the behaviour the later tasks rely on (nested construction,
version defaults, merge semantics).
"""

import pytest

from compchem_memory.hook.events import (
    EVENTS,
    SCHEMA_VERSION,
    AgentRef,
    Block,
    HookEvent,
    HookResponse,
    ToolCall,
    canonical_tool,
    normalise_output,
)


# --- canonical_tool -------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("compchem-tools_run_shell", ("compchem-tools", "run_shell")),
    ("mcp__compchem-tools__run_shell", ("compchem-tools", "run_shell")),
    ("compchem-memory_memory_search", ("compchem-memory", "memory_search")),
    ("write", (None, "write")),
    ("Edit", (None, "edit")),
])
def test_canonical_tool(raw, expected):
    assert canonical_tool(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("mcp__compchem-memory__memory_search", ("compchem-memory", "memory_search")),
    ("mcp__perspicacite__search", ("perspicacite", "search")),
    ("compchem-tools_read_file", ("compchem-tools", "read_file")),
    ("perspicacite_lookup", ("perspicacite", "lookup")),
    ("Bash", (None, "bash")),
    ("TodoWrite", (None, "todowrite")),
    # An underscore alone does not make a name an MCP tool: unknown prefixes
    # stay built-in so a client's own tool is never attributed to a server.
    ("todo_write", (None, "todo_write")),
    ("mcp__", (None, "mcp__")),
    ("mcp__lonely", (None, "mcp__lonely")),
])
def test_canonical_tool_extra_forms(raw, expected):
    assert canonical_tool(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("mcp_compchem-tools.run_shell", ("compchem-tools", "run_shell")),
    ("file_system.bash", (None, "bash")),
    ("file_system.write_file", (None, "write")),
    ("file_system.edit", (None, "edit")),
    ("agents.spawn", (None, "agents.spawn")),
    ("Bash", (None, "bash")),
])
def test_canonical_tool_vibe_forms(raw, expected):
    assert canonical_tool(raw) == expected


def test_canonical_tool_rejects_empty_name():
    with pytest.raises(ValueError):
        canonical_tool("")


# --- normalise_output -----------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("plain", "plain"),
    ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "a\nb"),
    ({"k": 1}, '{"k": 1}'),
    (None, None),
])
def test_normalise_output(value, expected):
    assert normalise_output(value) == expected


def test_normalise_output_skips_blocks_without_text():
    blocks = [
        {"type": "text", "text": "a"},
        {"type": "image", "source": {"data": "..."}},
        {"type": "text", "text": "b"},
    ]
    assert normalise_output(blocks) == "a\nb"


def test_normalise_output_empty_block_list_is_empty_string():
    assert normalise_output([]) == ""
    assert normalise_output([{"type": "image"}]) == ""


def test_normalise_output_keeps_non_ascii():
    assert normalise_output({"k": "é"}) == '{"k": "é"}'


# --- HookEvent.from_json --------------------------------------------------


def _payload(**overrides):
    payload = {"harness": "claude-code", "event": "prompt", "session_id": "s", "cwd": "/x"}
    payload.update(overrides)
    return payload


def test_from_json_rejects_unknown_event():
    with pytest.raises(ValueError):
        HookEvent.from_json({"harness": "json", "event": "boot", "session_id": "s", "cwd": "/x"})


def test_from_json_rejects_unsupported_version():
    with pytest.raises(ValueError):
        HookEvent.from_json({"harness": "json", "event": "prompt", "session_id": "s", "cwd": "/x",
                             "schema_version": 2})


def test_from_json_defaults_version_and_optionals():
    event = HookEvent.from_json(_payload(turn_id="t1", prompt="hello"))
    assert event.schema_version == SCHEMA_VERSION == 1
    assert event.harness == "claude-code"
    assert event.turn_id == "t1"
    assert event.prompt == "hello"
    assert event.tool is None
    assert event.agent is None
    assert event.assistant_text is None
    assert event.transcript_ref is None


def test_from_json_accepts_explicit_version_one():
    assert HookEvent.from_json(_payload(schema_version=1)).schema_version == 1


def test_from_json_rejects_missing_session_id():
    payload = _payload()
    del payload["session_id"]
    with pytest.raises(ValueError):
        HookEvent.from_json(payload)


@pytest.mark.parametrize("field", ["harness", "cwd"])
def test_from_json_rejects_missing_required_context(field):
    payload = _payload()
    del payload[field]
    with pytest.raises(ValueError):
        HookEvent.from_json(payload)


def test_from_json_builds_tool_from_raw_name():
    event = HookEvent.from_json(_payload(event="post-tool", tool={
        "raw_name": "mcp__compchem-tools__run_shell",
        "call_id": "call-1",
        "input": {"command": "ls"},
        "output": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
    }))
    assert event.tool == ToolCall(
        name="run_shell",
        server="compchem-tools",
        raw_name="mcp__compchem-tools__run_shell",
        call_id="call-1",
        input={"command": "ls"},
        output="a\nb",
        is_error=False,
    )


def test_from_json_keeps_explicit_tool_name_and_server():
    event = HookEvent.from_json(_payload(event="pre-tool", tool={
        "name": "run_shell",
        "server": "compchem-tools",
        "raw_name": "compchem-tools_run_shell",
        "input": {},
    }))
    assert event.tool.name == "run_shell"
    assert event.tool.server == "compchem-tools"
    assert event.tool.call_id is None
    assert event.tool.output is None


def test_from_json_tool_defaults_input_to_empty_dict():
    event = HookEvent.from_json(_payload(event="pre-tool", tool={"raw_name": "write"}))
    assert event.tool.name == "write"
    assert event.tool.server is None
    assert event.tool.input == {}


def test_from_json_keeps_error_flag():
    event = HookEvent.from_json(_payload(event="post-tool", tool={
        "raw_name": "write", "is_error": True, "output": "denied",
    }))
    assert event.tool.is_error is True
    assert event.tool.output == "denied"


def test_from_json_rejects_tool_without_raw_name():
    with pytest.raises(ValueError):
        HookEvent.from_json(_payload(event="pre-tool", tool={"name": "write"}))


def test_from_json_rejects_non_object_payload():
    with pytest.raises(ValueError):
        HookEvent.from_json(["not", "a", "dict"])


def test_from_json_rejects_blank_explicit_tool_name():
    with pytest.raises(ValueError):
        HookEvent.from_json(_payload(event="pre-tool", tool={"raw_name": "write", "name": ""}))


def test_from_json_builds_agent():
    event = HookEvent.from_json(_payload(event="agent-start", agent={"id": "a1", "type": "Explore"}))
    assert event.agent == AgentRef(id="a1", type="Explore")


def test_from_json_agent_type_is_optional():
    event = HookEvent.from_json(_payload(event="agent-start", agent={"id": "a1"}))
    assert event.agent == AgentRef(id="a1", type=None)


def test_from_json_rejects_agent_without_id():
    with pytest.raises(ValueError):
        HookEvent.from_json(_payload(event="agent-start", agent={"type": "Explore"}))


def test_hook_event_requires_known_event():
    with pytest.raises(ValueError):
        HookEvent(harness="json", event="boot", session_id="s", cwd="/x")


def test_events_constant_is_the_class_constant():
    assert EVENTS is HookEvent.EVENTS
    assert EVENTS == (
        "session-start", "prompt", "pre-tool", "post-tool",
        "turn-end", "session-end", "agent-start", "agent-end",
    )


# --- HookResponse.merge ---------------------------------------------------


def test_response_merge_joins_texts():
    r = HookResponse(context="a").merge(HookResponse(context="b", notice="n"))
    assert r.context == "a\n\nb" and r.notice == "n"


def test_response_merge_keeps_first_block():
    first, second = Block(reason="first"), Block(reason="second")
    merged = HookResponse(block=first).merge(HookResponse(block=second))
    assert merged.block is first


def test_response_merge_with_empty_side_is_identity():
    merged = HookResponse(context="a", notice="n", tool_note="t").merge(HookResponse())
    assert merged == HookResponse(context="a", notice="n", tool_note="t")


def test_response_merge_of_empty_responses_is_empty():
    assert HookResponse().merge(HookResponse()) == HookResponse()


def test_response_merge_does_not_mutate_operands():
    left = HookResponse(context="a")
    left.merge(HookResponse(context="b", block=Block(reason="r")))
    assert left == HookResponse(context="a")


def test_response_merge_joins_three_texts():
    merged = (HookResponse(context="a")
              .merge(HookResponse(context="b"))
              .merge(HookResponse(context="c")))
    assert merged.context == "a\n\nb\n\nc"


def test_lone_surrogates_are_replaced_at_the_boundary():
    event = HookEvent.from_json({
        "harness": "json", "event": "post-tool", "session_id": "s1", "cwd": "/p",
        "prompt": "ask \ud800 me", "assistant_text": "done \udc00",
        "tool": {"raw_name": "Read", "input": {"path": "x\ud83d"},
                 "output": [{"type": "text", "text": "out \ud83d"}, {"type": "text", "text": "\ud83d\ude00"}]},
    })

    assert event.prompt == "ask \ufffd me"
    assert event.assistant_text == "done \ufffd"
    assert event.tool.output == "out \ufffd\n\U0001f600"
    assert event.tool.input == {"path": "x\ufffd"}
    for text in (event.prompt, event.assistant_text, event.tool.output):
        text.encode("utf-8")
    assert normalise_output("a\ud800") == "a\ufffd"
    assert normalise_output({"k": "\udfff"}) == '{"k": "\ufffd"}'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Live finding (Codex 0.154): MCP server names arrive with "_" for "-".
        ("mcp__compchem_tools__run_shell", ("compchem-tools", "run_shell")),
        ("mcp__compchem_memory__memory_search", ("compchem-memory", "memory_search")),
        ("mcp_compchem_tools.run_shell", ("compchem-tools", "run_shell")),
        ("mcp__my_server__echo", ("my_server", "echo")),
    ],
)
def test_canonical_tool_restores_known_server_hyphens(raw, expected):
    assert canonical_tool(raw) == expected
