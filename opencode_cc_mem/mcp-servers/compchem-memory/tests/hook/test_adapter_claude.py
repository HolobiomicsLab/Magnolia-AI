"""Claude Code hook payload replay and native response rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compchem_memory.hook.adapters import ADAPTERS
from compchem_memory.hook.events import Block, HookResponse, normalise_output


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks" / "claude"
EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "prompt",
    "PreToolUse": "pre-tool",
    "PostToolUse": "post-tool",
    "PostToolUseFailure": "post-tool",
    "Stop": "turn-end",
    "SubagentStart": "agent-start",
    "SubagentStop": "agent-end",
    "SessionEnd": "session-end",
}


def _fixture_paths() -> list[Path]:
    return [
        path
        for path in sorted(FIXTURES.glob("*/*.json"))
        if path.name != "scenario.json" and "result-stream-line" not in path.name
    ]


def _event_payload(raw: dict) -> dict:
    native = raw["hook_event_name"]
    tool = None
    if raw.get("tool_name") not in (None, "ToolSearch", "SubagentHandback"):
        failure = native == "PostToolUseFailure"
        tool = {
            "raw_name": raw["tool_name"],
            "call_id": raw.get("tool_use_id"),
            "input": raw.get("tool_input"),
            "output": raw.get("error") if failure else normalise_output(raw.get("tool_response")),
            "is_error": failure,
        }
    agent = None
    if raw.get("agent_id") is not None:
        agent = {"id": raw["agent_id"], "type": raw.get("agent_type")}
    transcript = raw.get("agent_transcript_path") if native == "SubagentStop" else raw.get("transcript_path")
    return {
        "harness": "claude",
        "event": EVENTS[native],
        "session_id": raw["session_id"],
        "cwd": raw["cwd"],
        "turn_id": raw.get("prompt_id"),
        "prompt": raw.get("prompt"),
        "tool": tool,
        "agent": agent,
        "assistant_text": raw.get("last_assistant_message"),
        "transcript_ref": transcript,
    }


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda path: str(path.relative_to(FIXTURES)))
def test_replays_hook_fixtures(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))

    event = ADAPTERS["claude"].parse(raw, EVENTS[raw["hook_event_name"]])

    if raw.get("tool_name") in ("ToolSearch", "SubagentHandback"):
        assert event is None
        return
    assert event is not None
    expected = _event_payload(raw)
    assert event.harness == expected["harness"]
    assert event.event == expected["event"]
    assert event.session_id == expected["session_id"]
    assert event.cwd == expected["cwd"]
    assert event.turn_id == expected["turn_id"]
    assert event.prompt == expected["prompt"]
    assert event.assistant_text == expected["assistant_text"]
    assert event.transcript_ref == expected["transcript_ref"]
    if expected["agent"] is None:
        assert event.agent is None
    else:
        assert event.agent is not None
        assert event.agent.id == expected["agent"]["id"]
        assert event.agent.type == expected["agent"]["type"]
    if expected["tool"] is None:
        assert event.tool is None
    else:
        assert event.tool is not None
        assert event.tool.raw_name == expected["tool"]["raw_name"]
        assert event.tool.call_id == expected["tool"]["call_id"]
        assert event.tool.input == expected["tool"]["input"]
        assert event.tool.output == expected["tool"]["output"]
        assert event.tool.is_error == expected["tool"]["is_error"]


def test_parse_uses_cli_event_and_repairs_surrogates() -> None:
    event = ADAPTERS["claude"].parse(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "cwd": "/project",
            "tool_name": "Bash",
            "tool_input": {"command": "printf '\ud800'"},
            "tool_response": "bad \ud800 text",
        },
        "post-tool",
    )

    assert event is not None
    assert event.tool is not None
    assert event.tool.input["command"] == "printf '\ufffd'"
    assert event.tool.output == "bad \ufffd text"


def _render(response: HookResponse, event_name: str = "SessionStart"):
    event = ADAPTERS["claude"].parse(
        {"hook_event_name": event_name, "session_id": "s1", "cwd": "/project"},
        EVENTS[event_name],
    )
    assert event is not None
    return ADAPTERS["claude"].render(response, event)


def test_renders_context_tool_note_and_notice() -> None:
    rendered = _render(HookResponse(context="Context", tool_note="Tool note", notice="Notice"))

    assert json.loads(rendered.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": "Context\n\nTool note",
        },
        "systemMessage": "Notice",
    }
    assert rendered.stderr == ""
    assert rendered.exit_code == 0
    assert rendered.emitted == frozenset({"context", "tool_note", "notice"})


@pytest.mark.parametrize(
    ("tool_response", "native_name"),
    [("result", "PostToolUse"), (None, "PostToolUseFailure")],
)
def test_renders_post_tool_reverse_name(tool_response, native_name: str) -> None:
    event = ADAPTERS["claude"].parse(
        {
            "hook_event_name": native_name,
            "session_id": "s1",
            "cwd": "/project",
            "tool_name": "Bash",
            "tool_input": {},
            "tool_response": tool_response,
            "error": "failed" if tool_response is None else None,
        },
        "post-tool",
    )
    assert event is not None

    rendered = ADAPTERS["claude"].render(HookResponse(tool_note="Note"), event)

    assert json.loads(rendered.stdout)["hookSpecificOutput"] == {
        "hookEventName": native_name,
        "additionalContext": "Note",
    }


def test_renders_deny_without_failing_process() -> None:
    rendered = _render(HookResponse(block=Block("Shell is disabled")), "PreToolUse")

    assert json.loads(rendered.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Shell is disabled",
        }
    }
    assert rendered.exit_code == 0
    assert rendered.stderr == ""
    assert rendered.emitted == frozenset()


@pytest.mark.parametrize("event_name", ["Stop", "SessionEnd", "SubagentStart", "SubagentStop"])
def test_terminal_and_agent_events_emit_nothing(event_name: str) -> None:
    raw = {"hook_event_name": event_name, "session_id": "s1", "cwd": "/project"}
    if event_name.startswith("Subagent"):
        raw["agent_id"] = "a1"
    event = ADAPTERS["claude"].parse(raw, EVENTS[event_name])
    assert event is not None

    rendered = ADAPTERS["claude"].render(
        HookResponse(context="Context", tool_note="Note", notice="Notice", block=Block("No")), event
    )

    assert rendered.stdout == rendered.stderr == ""
    assert rendered.exit_code == 0
    assert rendered.emitted == frozenset()


def test_block_and_context_render_one_document() -> None:
    rendered = _render(HookResponse(context="Context", block=Block("No shell")), "PreToolUse")

    assert json.loads(rendered.stdout) == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "Context",
        "permissionDecision": "deny",
        "permissionDecisionReason": "No shell",
    }}
    assert rendered.emitted == frozenset({"context"})
