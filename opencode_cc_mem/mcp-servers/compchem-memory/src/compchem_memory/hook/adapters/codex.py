"""Codex hooks, using Claude-compatible output with inline notices."""

from __future__ import annotations

from compchem_memory.hook.events import (
    HookEvent,
    HookResponse,
    ToolTarget,
    canonical_tool,
)

from . import Rendered, claude


def parse(raw: dict, event: str) -> HookEvent | None:
    """Translate Codex's Claude-shaped payload, including its native turn ID."""
    translated = dict(raw)
    translated["prompt_id"] = raw.get("turn_id")
    payload = claude.translated_payload(translated, event, "codex")
    if payload is None:
        return None
    parsed = HookEvent.from_json(payload)
    _decode_policy_targets(parsed)
    return parsed


def render(resp: HookResponse, event: HookEvent) -> Rendered:
    """Render notices as additional context instead of Claude's system message."""
    if resp.notice is None:
        return claude.render(resp, event)
    notice = f"[Magnolia notice] {resp.notice}"
    rendered = claude.render(HookResponse(
        context=_join(resp.context, notice) if resp.tool_note is None else resp.context,
        notice=None,
        tool_note=None if resp.tool_note is None else _join(resp.tool_note, notice),
        block=resp.block,
    ), event)
    if not rendered.stdout:
        return rendered
    emitted = {"notice"}
    emitted |= {field for field in ("context", "tool_note") if getattr(resp, field) is not None}
    return Rendered(rendered.stdout, rendered.stderr, rendered.exit_code, frozenset(emitted))


def _join(first: str | None, second: str) -> str:
    return second if first is None else f"{first}\n\n{second}"


def _decode_policy_targets(event: HookEvent) -> None:
    """Decode only synthetic-only Codex shapes recorded for Task 6."""
    tool = event.tool
    if tool is None:
        return
    server, name = canonical_tool(tool.raw_name)
    if server is not None:
        tool.policy_action = "mcp"
        return
    if name != "read":
        return
    path = tool.input.get("file_path")
    if isinstance(path, str) and path:
        tool.policy_action = "native_read"
        tool.targets = (ToolTarget(path, "read"),)
