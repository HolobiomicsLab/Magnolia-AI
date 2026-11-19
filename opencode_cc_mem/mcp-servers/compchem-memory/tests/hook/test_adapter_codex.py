"""Codex hook payload replay and Claude-compatible response rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compchem_memory.hook.adapters import ADAPTERS
from compchem_memory.hook.events import Block, HookResponse, normalise_output


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks" / "codex"
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
    paths = []
    for path in sorted(FIXTURES.glob("*/*.json")):
        if path.parent.name in ("parent-events", "mcp-events-no-hooks"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "hook_event_name" in raw:
            paths.append(path)
    return paths


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda path: str(path.relative_to(FIXTURES)))
def test_replays_hook_fixtures(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    native = raw["hook_event_name"]

    event = ADAPTERS["codex"].parse(raw, EVENTS[native])

    if raw.get("tool_name") in ("ToolSearch", "SubagentHandback"):
        assert event is None
        return
    assert event is not None
    assert event.harness == "codex"
    assert event.event == EVENTS[native]
    assert event.session_id == raw["session_id"]
    assert event.cwd == raw["cwd"]
    assert event.turn_id == raw.get("turn_id")
    assert event.prompt == raw.get("prompt")
    assert event.assistant_text == raw.get("last_assistant_message")
    expected_transcript = raw.get("agent_transcript_path") if native == "SubagentStop" else raw.get("transcript_path")
    assert event.transcript_ref == expected_transcript
    if raw.get("agent_id") is None:
        assert event.agent is None
    else:
        assert event.agent is not None
        assert (event.agent.id, event.agent.type) == (raw["agent_id"], raw.get("agent_type"))
    if raw.get("tool_name") is None:
        assert event.tool is None
    else:
        assert event.tool is not None
        failure = native == "PostToolUseFailure"
        assert event.tool.raw_name == raw["tool_name"]
        assert event.tool.call_id == raw.get("tool_use_id")
        assert event.tool.input == raw["tool_input"]
        expected_output = raw.get("error") if failure else normalise_output(raw.get("tool_response"))
        assert event.tool.output == expected_output
        assert event.tool.is_error is failure


def _render(response: HookResponse, native_name: str = "SessionStart"):
    raw = {"hook_event_name": native_name, "session_id": "s1", "cwd": "/project"}
    if native_name in ("PreToolUse", "PostToolUse"):
        raw.update(tool_name="Bash", tool_input={}, tool_response="result")
    event = ADAPTERS["codex"].parse(raw, EVENTS[native_name])
    assert event is not None
    return ADAPTERS["codex"].render(response, event)


def test_renders_context_tool_note_and_notice() -> None:
    rendered = _render(HookResponse(context="Context", tool_note="Tool note", notice="Notice"))

    assert json.loads(rendered.stdout) == {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "Context\n\nTool note\n\n[Magnolia notice] Notice",
    }}
    assert rendered.exit_code == 0
    assert rendered.stderr == ""
    assert rendered.emitted == frozenset({"context", "tool_note", "notice"})


def test_renders_deny() -> None:
    rendered = _render(HookResponse(block=Block("Shell is disabled")), "PreToolUse")

    assert json.loads(rendered.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Shell is disabled",
        }
    }
    assert rendered.exit_code == 0
    assert rendered.emitted == frozenset()


def test_failure_post_tool_uses_failure_reverse_name() -> None:
    event = ADAPTERS["codex"].parse(
        {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "s1",
            "cwd": "/project",
            "tool_name": "Bash",
            "tool_input": {},
            "error": "failed",
        },
        "post-tool",
    )
    assert event is not None

    rendered = ADAPTERS["codex"].render(HookResponse(tool_note="Note"), event)

    assert json.loads(rendered.stdout)["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


@pytest.mark.parametrize("event_name", ["Stop", "SessionEnd"])
def test_terminal_events_emit_nothing(event_name: str) -> None:
    rendered = _render(HookResponse(context="Context", notice="Notice"), event_name)

    assert rendered.stdout == rendered.stderr == ""
    assert rendered.exit_code == 0
    assert rendered.emitted == frozenset()


def test_notice_alone_is_not_reported_as_a_tool_note() -> None:
    rendered = _render(HookResponse(notice="Job done"))

    assert json.loads(rendered.stdout)["hookSpecificOutput"]["additionalContext"] == (
        "[Magnolia notice] Job done"
    )
    assert rendered.emitted == frozenset({"notice"})
