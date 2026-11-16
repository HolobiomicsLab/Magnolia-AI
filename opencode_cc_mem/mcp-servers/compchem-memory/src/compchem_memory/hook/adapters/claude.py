"""Claude Code's hook payloads and JSON control protocol."""

from __future__ import annotations

import json

from compchem_memory.hook.events import (
    HookEvent,
    HookResponse,
    ToolTarget,
    canonical_tool,
    normalise_output,
)

from . import Rendered

_IGNORED_TOOLS = frozenset({"ToolSearch", "SubagentHandback"})
_SILENT_EVENTS = frozenset({"turn-end", "session-end", "agent-start", "agent-end"})


def parse(raw: dict, event: str) -> HookEvent | None:
    """Translate Claude's payload, ignoring deferred-tool discovery events."""
    payload = translated_payload(raw, event, "claude")
    if payload is None:
        return None
    parsed = HookEvent.from_json(payload)
    _decode_policy_targets(parsed)
    return parsed


def translated_payload(raw: dict, event: str, harness: str) -> dict | None:
    """Return the neutral payload shared by Claude-shaped client adapters."""
    if raw.get("tool_name") in _IGNORED_TOOLS:
        return None
    native = raw.get("hook_event_name")
    values = {
        "harness": harness,
        "event": event,
        "session_id": raw.get("session_id"),
        "cwd": raw.get("cwd"),
        "turn_id": raw.get("prompt_id"),
        "prompt": raw.get("prompt"),
        "assistant_text": raw.get("last_assistant_message"),
        "transcript_ref": _transcript_ref(raw, native),
        "agent": _agent(raw),
        "tool": _tool(raw, native),
    }
    return values


def render(resp: HookResponse, event: HookEvent) -> Rendered:
    """Render one JSON object: context and tool note, system message, denial.

    Claude Code parses stdout as a single JSON object, so every field goes
    into the same document.
    """
    if event.event in _SILENT_EVENTS:
        return Rendered("", "", 0, frozenset())
    emitted = set()
    texts = []
    for field in ("context", "tool_note"):
        text = getattr(resp, field)
        if text is not None:
            texts.append(text)
            emitted.add(field)
    specific: dict = {}
    if texts:
        specific["additionalContext"] = "\n\n".join(texts)
    if resp.block is not None:
        specific["permissionDecision"] = "deny"
        specific["permissionDecisionReason"] = resp.block.reason
    document: dict = {}
    if specific:
        document["hookSpecificOutput"] = {"hookEventName": _native_name(event), **specific}
    if resp.notice is not None:
        document["systemMessage"] = resp.notice
        emitted.add("notice")
    stdout = f"{json.dumps(document, ensure_ascii=False)}\n" if document else ""
    return Rendered(stdout, "", 0, frozenset(emitted))


def _native_name(event: HookEvent) -> str:
    names = {
        "session-start": "SessionStart",
        "prompt": "UserPromptSubmit",
        "pre-tool": "PreToolUse",
        "turn-end": "Stop",
        "session-end": "SessionEnd",
        "agent-start": "SubagentStart",
        "agent-end": "SubagentStop",
    }
    if event.event != "post-tool":
        return names[event.event]
    failed = event.tool is not None and event.tool.is_error
    return "PostToolUseFailure" if failed else "PostToolUse"


def _transcript_ref(raw: dict, native: object) -> object:
    if native == "SubagentStop":
        return raw.get("agent_transcript_path")
    return raw.get("transcript_path")


def _agent(raw: dict) -> dict | None:
    if raw.get("agent_id") is None:
        return None
    return {"id": raw["agent_id"], "type": raw.get("agent_type")}


def _tool(raw: dict, native: object) -> dict | None:
    if raw.get("tool_name") is None:
        return None
    failed = native == "PostToolUseFailure"
    output = raw.get("error") if failed else normalise_output(raw.get("tool_response"))
    return {
        "raw_name": raw["tool_name"],
        "call_id": raw.get("tool_use_id"),
        "input": raw.get("tool_input"),
        "output": output,
        "is_error": failed,
    }


def _decode_policy_targets(event: HookEvent) -> None:
    """Decode only fixture-backed Claude native path shapes."""
    tool = event.tool
    if tool is None:
        return
    server, name = canonical_tool(tool.raw_name)
    if server is not None:
        tool.policy_action = "mcp"
        return
    if name == "read":
        _set_path(tool, "native_read", "file_path", "read")
    elif name == "glob":
        _set_search(tool, event.cwd, "native_glob", "pattern")
    elif name == "grep":
        _set_search(tool, event.cwd, "native_grep", "glob")
    elif name in {"edit", "write"}:
        _set_path(tool, "native_edit", "file_path", "edit")


def _set_path(tool: object, action: str, field: str, access: str) -> None:
    value = tool.input.get(field)
    if not isinstance(value, str) or not value:
        return
    tool.policy_action = action
    tool.targets = (ToolTarget(value, access),)


def _set_search(tool: object, cwd: str, action: str, inclusion: str) -> None:
    root = tool.input.get("path", cwd)
    if not isinstance(root, str) or not root:
        return
    included = tool.input.get(inclusion)
    inclusions = (included,) if isinstance(included, str) and included else ()
    tool.policy_action = action
    tool.targets = (
        ToolTarget(root, "search", broad=True, inclusions=inclusions),
    )


def _additional_context(native_name: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": native_name, "additionalContext": text}}
