"""Claude Code's hook payloads and JSON control protocol."""

from __future__ import annotations

import json

from compchem_memory.hook.events import HookEvent, HookResponse, normalise_output

from . import Rendered

_IGNORED_TOOLS = frozenset({"ToolSearch", "SubagentHandback"})
_SILENT_EVENTS = frozenset({"turn-end", "session-end", "agent-start", "agent-end"})


def parse(raw: dict, event: str) -> HookEvent | None:
    """Translate Claude's payload, ignoring deferred-tool discovery events."""
    payload = translated_payload(raw, event, "claude")
    return None if payload is None else HookEvent.from_json(payload)


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


def _additional_context(native_name: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": native_name, "additionalContext": text}}
