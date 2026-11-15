"""Render a Claude Code launch: settings, MCP config and system prompt, passed by flag."""

from __future__ import annotations

import json
from pathlib import Path

from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.protocol import Launch, LaunchContext
from compchem_memory.harness.render_common import (
    MCP_TOOL_TIMEOUT_MS,
    active_servers,
    harness_dir,
    hook_command,
    hook_timeout,
    policy_files,
    skill_links,
    split_command,
)

HARNESS = "claude"
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
_ASYNC_EVENTS: set[str] = set()
_TOOL_NAMES = {"bash": "Bash"}
_MAGNOLIA_PERMISSIONS = ["mcp__compchem-tools", "mcp__compchem-memory"]


def render(neutral: NeutralConfig, context: LaunchContext) -> Launch:
    folder = harness_dir(context, HARNESS)
    files, env = policy_files(neutral, context, HARNESS)
    files[folder / "settings.json"] = _settings(folder / "settings.json", context)
    files[folder / "mcp.json"] = _json({"mcpServers": _servers(neutral)})
    files[folder / "system-prompt.md"] = _system_prompt(context.root).encode("utf-8")
    denied = ",".join(_TOOL_NAMES.get(name, name) for name in sorted(neutral.deny_tools))
    argv = [
        "claude", "--setting-sources", "project,local",
        "--settings", str(folder / "settings.json"),
        "--mcp-config", str(folder / "mcp.json"), "--strict-mcp-config",
    ]
    if denied:
        argv += ["--disallowedTools", denied]
    argv += ["--append-system-prompt-file", str(folder / "system-prompt.md")]
    if context.model:
        argv += ["--model", context.model]
    env["MCP_TOOL_TIMEOUT"] = MCP_TOOL_TIMEOUT_MS
    return Launch(
        argv=argv,
        env=env,
        files=files,
        links=skill_links(context, ".claude/skills"),
    )


render.binary = "claude"


def _hooks(context: LaunchContext) -> dict:
    hooks = {}
    for event, name in EVENTS.items():
        hook = {
            "type": "command",
            "command": hook_command(context, HARNESS, name),
            "timeout": hook_timeout(name),
        }
        if event in _ASYNC_EVENTS:
            hook["async"] = True
        hooks[event] = [{"hooks": [hook]}]
    return hooks


def _settings(path: Path, context: LaunchContext) -> bytes:
    """Keep member settings while replacing Magnolia hooks and permissions."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    permissions = current.get("permissions")
    permissions = dict(permissions) if isinstance(permissions, dict) else {}
    allowed = permissions.get("allow")
    member = [item for item in allowed if item not in _MAGNOLIA_PERMISSIONS] \
        if isinstance(allowed, list) else []
    permissions["allow"] = [*member, *_MAGNOLIA_PERMISSIONS]
    current.update(hooks=_hooks(context), permissions=permissions)
    return _json(current)


def _servers(neutral: NeutralConfig) -> dict:
    servers = {}
    for server in active_servers(neutral):
        if server.kind == "remote":
            servers[server.name] = {"type": "http", "url": server.url}
            continue
        command, args = split_command(server)
        entry = {"command": command, "args": args}
        if server.env:
            entry["env"] = dict(server.env)
        servers[server.name] = entry
    return servers


def _system_prompt(root: Path) -> str:
    sections = [_read(root / "AGENTS.md")]
    rules = root / "rules"
    names = sorted(path.name for path in rules.iterdir() if path.name.endswith(".md")) if rules.is_dir() else []
    for name in names:
        text = _read(rules / name)
        if text:
            sections.append(f"--- rules/{name} ---\n{text}")
    return "\n\n".join(section for section in sections if section) + "\n"


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _json(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
