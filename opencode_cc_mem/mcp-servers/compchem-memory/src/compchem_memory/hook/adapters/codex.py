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
    """Decode fixture-backed Codex native and Magnolia read-server targets."""
    tool = event.tool
    if tool is None:
        return
    server, name = canonical_tool(tool.raw_name)
    if server is not None:
        tool.policy_action = "mcp"
        if server == "magnolia-read" and name in {"read_file", "list_dir", "search"}:
            path = tool.input.get("path")
            if isinstance(path, str) and path:
                broad = name == "search"
                tool.targets = (
                    ToolTarget(path, "search" if broad else "read", broad=broad),
                )
        return
    if name == "read":
        path = tool.input.get("file_path")
        if isinstance(path, str) and path:
            tool.policy_action = "native_read"
            tool.targets = (ToolTarget(path, "read"),)
        return
    if name == "apply_patch":
        patch = tool.input.get("command")
        if not isinstance(patch, str):
            return
        paths = _patch_paths(patch)
        if paths is None:
            return
        tool.policy_action = "native_edit"
        tool.targets = tuple(ToolTarget(value, "edit") for value in paths)


def _patch_paths(patch: str) -> list[str] | None:
    """Decode the headers accepted by Codex 0.154, or fail closed."""
    lines = _patch_lines(patch)
    if lines is None:
        return None
    stripped = [line.strip() for line in lines if line.strip()]
    if not stripped or stripped[0] != "*** Begin Patch":
        return None
    if stripped[-1] != "*** End Patch":
        return None
    prefixes = (
        "*** Update File: ",
        "*** Add File: ",
        "*** Delete File: ",
        "*** Move to: ",
    )
    markers = {"*** Begin Patch", "*** End Patch", "*** End of File"}
    paths: list[str] = []
    for line in lines:
        header = line.lstrip()
        if not header.startswith("***"):
            continue
        if header.strip() in markers:
            continue
        matched = next((prefix for prefix in prefixes if header.startswith(prefix)), None)
        if matched is None:
            return None
        path = header.removeprefix(matched)
        if not path or path != path.strip():
            return None
        paths.append(path)
    return paths or None


def _patch_lines(patch: str) -> list[str] | None:
    """Split like Codex and reject every non-tab C0/C1 control."""
    lines: list[str] = []
    for raw_line in patch.split("\n"):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if any(_forbidden_patch_character(character) for character in line):
            return None
        lines.append(line)
    return lines


def _forbidden_patch_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        (codepoint < 0x20 and character != "\t")
        or 0x7F <= codepoint <= 0x9F
        or character in {"\u2028", "\u2029"}
    )
