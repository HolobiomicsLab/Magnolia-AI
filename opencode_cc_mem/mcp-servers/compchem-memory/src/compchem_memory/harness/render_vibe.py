"""Render a Vibe launch: project ``.vibe`` config and hooks, merged with the member's.

``disabled_tools`` is append-only: Magnolia adds the tools its deny set names (the
shell, fixed) when the member's list lacks them, and never removes one, so a stale
``file_system.bash`` is not cleaned up and cannot be told from a member's own entry.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from compchem_memory.harness.files import merged_toml, toml_bytes, validate_project_paths
from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.ownership import owned
from compchem_memory.harness.protocol import Launch, LaunchContext
from compchem_memory.harness.render_common import (
    active_servers,
    hook_command,
    hook_timeout,
    policy_files,
    skill_links,
    split_command,
    timeout_seconds,
)

HARNESS = "vibe"
# (hook type, neutral event); the hook name is ``magnolia-<neutral event>``.
EVENTS = (
    ("session_start", "session-start"),
    ("pre_agent", "prompt"),
    ("pre_tool", "pre-tool"),
    ("post_tool", "post-tool"),
    ("post_agent", "turn-end"),
    ("session_end", "session-end"),
)
_TOOL_NAMES = {"bash": "file_system.bash"}


def render(neutral: NeutralConfig, context: LaunchContext) -> Launch:
    files, env = policy_files(neutral, context, HARNESS)
    folder = context.project_dir / ".vibe"
    conflicts: list[str] = []
    config_path = folder / "config.toml"
    hooks_path = folder / "hooks.toml"
    validate_project_paths((config_path, hooks_path), context.project_dir)
    content, found, original = merged_toml(
        config_path, owned, _config(neutral, context, config_path)
    )
    files[config_path] = content
    conflicts += found
    _keep_backup(files, config_path, original)
    content, found, original = merged_toml(
        hooks_path, owned, {"hooks": _hooks(context)}
    )
    files[hooks_path] = content
    conflicts += found
    _keep_backup(files, hooks_path, original)
    return Launch(
        argv=["vibe", "--workdir", str(context.project_dir)],
        env=env,
        files=files,
        conflicts=conflicts,
        links=skill_links(context, ".agents/skills"),
    )


render.binary = "vibe"


def native_artifacts(
    context: LaunchContext,
    mcp_servers: list[Mapping[str, Any]],
) -> dict[str, bytes]:
    """Render immutable Vibe project files for one governed invocation."""
    config: dict[str, Any] = {
        "active_model": context.model or "",
        "disabled_tools": ["file_system.bash"],
        "mcp_servers": [dict(server) for server in mcp_servers],
    }
    return {
        ".vibe/config.toml": toml_bytes(config),
        ".vibe/hooks.toml": toml_bytes({"hooks": _hooks(context)}),
    }


def _keep_backup(files: dict[Path, bytes], path: Path, original: bytes | None) -> None:
    backup = path.with_name(path.name + ".magnolia-backup")
    if original is not None and not backup.exists():
        files[backup] = original


def _config(neutral: NeutralConfig, context: LaunchContext, path: Path) -> dict:
    config: dict = {"mcp_servers": _servers(neutral)}
    if context.model:
        config["active_model"] = context.model
    # Lists merge by appending, so only add the names the member's file lacks.
    present = _existing_disabled_tools(path)
    disabled = [
        _TOOL_NAMES.get(name, name)
        for name in sorted(neutral.deny_tools)
        if _TOOL_NAMES.get(name, name) not in present
    ]
    if disabled:
        config["disabled_tools"] = disabled
    return config


def _existing_disabled_tools(path: Path) -> list[str]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8")).get("disabled_tools", [])
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return value if isinstance(value, list) else []


def _servers(neutral: NeutralConfig) -> list[dict]:
    servers = []
    for server in active_servers(neutral):
        if server.kind == "remote":
            entry = {"name": server.name, "transport": "http", "url": server.url}
        else:
            command, args = split_command(server)
            entry = {"name": server.name, "transport": "stdio", "command": command}
            if args:
                entry["args"] = args
            if server.env:
                entry["env"] = dict(server.env)
        entry["tool_timeout_sec"] = timeout_seconds(server)
        servers.append(entry)
    return servers


def _hooks(context: LaunchContext) -> list[dict]:
    hooks = []
    for kind, event in EVENTS:
        hook = {
            "name": f"magnolia-{event}",
            "type": kind,
            "command": hook_command(context, HARNESS, event),
            "timeout": hook_timeout(event),
        }
        if kind == "pre_tool":
            hook["strict"] = True
        if kind == "pre_agent":
            hook["match"] = "main"
        hooks.append(hook)
    return hooks
