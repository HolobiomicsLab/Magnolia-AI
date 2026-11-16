"""Vibe's lower-case hooks and snake-case JSON response protocol.

Vibe drops post-tool context for MCP tools, and replacing their result destroys
the structured value read by ``run_typescript``. Magnolia therefore renders
nothing for every MCP post-tool and carries its context to a later event.
"""

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

_CONTEXT_EVENTS = frozenset({"session-start", "prompt", "post-tool"})


def parse(raw: dict, event: str) -> HookEvent | None:
    """Translate Vibe's payload and bind child events to their parent session."""
    if raw.get("hook_event_name") == "notification":
        return None
    child = raw.get("session_id") if raw.get("parent_session_id") else None
    agent = {"id": child, "type": raw.get("agent_name")} if child else None
    parsed = HookEvent.from_json({
        "harness": "vibe",
        "event": event,
        "session_id": raw.get("parent_session_id") or raw.get("session_id"),
        "cwd": raw.get("cwd"),
        "turn_id": raw.get("turn_id"),
        "prompt": raw.get("prompt"),
        "transcript_ref": raw.get("transcript_path"),
        "agent": agent,
        "tool": _tool(raw),
    })
    _decode_policy_targets(parsed)
    return parsed


def render(resp: HookResponse, event: HookEvent) -> Rendered:
    """Render Vibe's event-limited context and pre-tool denial objects."""
    if resp.block is not None and event.event == "pre-tool":
        payload = {"decision": "deny", "reason": resp.block.reason}
        return Rendered(f"{json.dumps(payload, ensure_ascii=False)}\n", "", 0, frozenset())
    if event.event not in _CONTEXT_EVENTS:
        return Rendered("", "", 0, frozenset())
    if _is_mcp_post_tool(event):
        return Rendered("", "", 0, frozenset())
    fields = ("context", "tool_note") if event.event == "post-tool" else ("context",)
    texts = [(field, getattr(resp, field)) for field in fields]
    if resp.notice is not None:
        texts.append(("notice", f"[Magnolia notice] {resp.notice}"))
    present = [(field, text) for field, text in texts if text is not None]
    if not present:
        return Rendered("", "", 0, frozenset())
    text = "\n\n".join(value for _, value in present)
    payload = {"hook_specific_output": {"additional_context": text}}
    return Rendered(
        f"{json.dumps(payload, ensure_ascii=False)}\n", "", 0, frozenset(field for field, _ in present)
    )


def _tool(raw: dict) -> dict | None:
    if raw.get("tool_name") is None:
        return None
    failed = raw.get("tool_status") == "failure"
    output = raw.get("tool_error") if failed else raw.get("tool_output_text")
    if not failed and output is None:
        output = raw.get("tool_output")
    return {
        "raw_name": raw["tool_name"],
        "call_id": raw.get("tool_call_id"),
        "input": raw.get("tool_input"),
        "output": normalise_output(output),
        "is_error": failed,
    }


def _is_mcp_post_tool(event: HookEvent) -> bool:
    """Whether Vibe named this post-tool event with its MCP wire shape."""
    if event.event != "post-tool" or event.tool is None:
        return False
    raw_name = event.tool.raw_name
    return raw_name.startswith("mcp_") and "." in raw_name.removeprefix("mcp_")


def _decode_policy_targets(event: HookEvent) -> None:
    """Decode only synthetic-only Vibe file-system shapes from Task 6."""
    tool = event.tool
    if tool is None:
        return
    server, name = canonical_tool(tool.raw_name)
    if server is not None:
        tool.policy_action = "mcp"
        return
    shape = {
        "read_file": ("native_read", "read"),
        "write": ("native_edit", "edit"),
        "edit": ("native_edit", "edit"),
    }.get(name)
    if shape is None:
        return
    path = tool.input.get("path")
    if isinstance(path, str) and path:
        tool.policy_action = shape[0]
        tool.targets = (ToolTarget(path, shape[1]),)
