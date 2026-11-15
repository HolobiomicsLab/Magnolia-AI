"""Vibe hook payload replay and snake-case response rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compchem_memory.hook.adapters import ADAPTERS
from compchem_memory.hook.events import Block, HookResponse, normalise_output


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks" / "vibe"
EVENTS = {
    "session_start": "session-start",
    "pre_agent": "prompt",
    "pre_tool": "pre-tool",
    "post_tool": "post-tool",
    "post_agent": "turn-end",
    "session_end": "session-end",
}


def _fixture_paths() -> list[Path]:
    paths = []
    for path in sorted(FIXTURES.glob("*/*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "hook_event_name" in raw:
            paths.append(path)
    return paths


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda path: str(path.relative_to(FIXTURES)))
def test_replays_hook_fixtures(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    native = raw["hook_event_name"]

    event = ADAPTERS["vibe"].parse(raw, EVENTS.get(native, "session-start"))

    if native == "notification":
        assert event is None
        return
    assert event is not None
    assert event.harness == "vibe"
    assert event.event == EVENTS[native]
    assert event.session_id == (raw.get("parent_session_id") or raw["session_id"])
    assert event.cwd == raw["cwd"]
    assert event.turn_id == raw.get("turn_id")
    assert event.prompt == raw.get("prompt")
    assert event.transcript_ref == raw.get("transcript_path")
    if raw.get("parent_session_id") is None:
        assert event.agent is None
    else:
        assert event.agent is not None
        assert (event.agent.id, event.agent.type) == (raw["session_id"], raw.get("agent_name"))
    if raw.get("tool_name") is None:
        assert event.tool is None
    else:
        assert event.tool is not None
        failure = raw.get("tool_status") == "failure"
        output = raw.get("tool_error") if failure else raw.get("tool_output_text")
        if not failure and output is None:
            output = normalise_output(raw.get("tool_output"))
        assert event.tool.raw_name == raw["tool_name"]
        assert event.tool.call_id == raw.get("tool_call_id")
        assert event.tool.input == raw["tool_input"]
        assert event.tool.output == normalise_output(output)
        assert event.tool.is_error is failure


def _render(response: HookResponse, native_name: str = "session_start"):
    raw = {"hook_event_name": native_name, "session_id": "s1", "cwd": "/project"}
    if native_name in ("pre_tool", "post_tool"):
        raw.update(tool_name="file_system.bash", tool_input={}, tool_output_text="result")
    event = ADAPTERS["vibe"].parse(raw, EVENTS[native_name])
    assert event is not None
    return ADAPTERS["vibe"].render(response, event)


@pytest.mark.parametrize("native_name", ["session_start", "pre_agent", "post_tool"])
def test_renders_context_and_notice(native_name: str) -> None:
    rendered = _render(HookResponse(context="Context", notice="Notice"), native_name)

    assert json.loads(rendered.stdout) == {
        "hook_specific_output": {"additional_context": "Context\n\n[Magnolia notice] Notice"}
    }
    assert rendered.exit_code == 0
    assert rendered.stderr == ""
    assert rendered.emitted == frozenset({"context", "notice"})


def test_renders_tool_note_on_post_tool() -> None:
    rendered = _render(HookResponse(tool_note="Tool note"), "post_tool")

    assert json.loads(rendered.stdout) == {"hook_specific_output": {"additional_context": "Tool note"}}
    assert rendered.emitted == frozenset({"tool_note"})


def test_mcp_post_tool_replaces_result_with_original_output_and_context() -> None:
    raw = {
        "hook_event_name": "post_tool", "session_id": "s1", "cwd": "/project",
        "tool_name": "mcp_compchem-tools.run_shell", "tool_input": {},
        "tool_output_text": "original output\nkept verbatim",
    }
    event = ADAPTERS["vibe"].parse(raw, "post-tool")
    assert event is not None

    rendered = ADAPTERS["vibe"].render(
        HookResponse(context="Context", tool_note="Tool note", notice="Notice"), event,
    )

    assert json.loads(rendered.stdout) == {
        "decision": "deny",
        "reason": (
            "original output\nkept verbatim\n\n"
            "Context\n\nTool note\n\n[Magnolia notice] Notice"
        ),
    }
    assert rendered.emitted == frozenset({"context", "tool_note", "notice"})


@pytest.mark.parametrize("native_output", [None, "failure"])
def test_mcp_post_tool_without_original_text_uses_additional_context(native_output) -> None:
    raw = {
        "hook_event_name": "post_tool", "session_id": "s1", "cwd": "/project",
        "tool_name": "mcp_compchem-tools.run_shell", "tool_input": {},
        "tool_output_text": native_output,
    }
    if native_output == "failure":
        raw.update(tool_status="failure", tool_error="failed")
    event = ADAPTERS["vibe"].parse(raw, "post-tool")
    assert event is not None

    rendered = ADAPTERS["vibe"].render(HookResponse(tool_note="Tool note"), event)

    assert json.loads(rendered.stdout) == {
        "hook_specific_output": {"additional_context": "Tool note"}
    }


def test_builtin_post_tool_keeps_additional_context() -> None:
    rendered = _render(HookResponse(tool_note="Tool note"), "post_tool")

    assert json.loads(rendered.stdout) == {
        "hook_specific_output": {"additional_context": "Tool note"}
    }


def test_renders_deny() -> None:
    rendered = _render(HookResponse(block=Block("Shell is disabled")), "pre_tool")

    assert json.loads(rendered.stdout) == {"decision": "deny", "reason": "Shell is disabled"}
    assert rendered.exit_code == 0
    assert rendered.stderr == ""
    assert rendered.emitted == frozenset()


@pytest.mark.parametrize("native_name", ["pre_tool", "post_agent", "session_end"])
def test_other_events_do_not_emit_context(native_name: str) -> None:
    rendered = _render(HookResponse(context="Context", tool_note="Note", notice="Notice"), native_name)

    assert rendered.stdout == rendered.stderr == ""
    assert rendered.exit_code == 0
    assert rendered.emitted == frozenset()


def test_notification_is_ignored_even_with_cli_event_name() -> None:
    raw = {"hook_event_name": "notification", "session_id": "s1", "cwd": "/project"}

    assert ADAPTERS["vibe"].parse(raw, "session-start") is None
