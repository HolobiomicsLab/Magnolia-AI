"""Render a Vibe launch: project ``.vibe`` config and hooks, merged with the member's.

``disabled_tools`` is append-only: Magnolia adds Vibe's Python catalogue names for
its neutral deny set when the member's list lacks them, and never removes one, so a
stale ``file_system.bash`` from an older render is kept beside the added ``bash``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from functools import partial
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
_owns_entry = partial(owned, client=HARNESS)
# (hook type, neutral event); the hook name is ``magnolia-<neutral event>``.
EVENTS = (
    ("session_start", "session-start"),
    ("pre_agent", "prompt"),
    ("pre_tool", "pre-tool"),
    ("post_tool", "post-tool"),
    ("post_agent", "turn-end"),
    ("session_end", "session-end"),
)
_TOOL_NAMES = {
    "bash": "bash",
    "write": "write_file",
    "write_file": "write_file",
    "edit": "edit",
}
READ_ONLY_DISABLED_TOOLS = (
    "bash",
    "edit",
    "write_file",
)


def disabled_tools_for_role(role: str) -> tuple[str, ...]:
    """Return the native tools disabled for one capability-admitted role."""
    if role == "worker":
        return ("bash",)
    if role in {"reviewer", "helper", "evaluator"}:
        return READ_ONLY_DISABLED_TOOLS
    raise ValueError(f"unknown Vibe role: {role}")


def render(neutral: NeutralConfig, context: LaunchContext) -> Launch:
    files, env = policy_files(neutral, context, HARNESS)
    folder = context.project_dir / ".vibe"
    conflicts: list[str] = []
    config_path = folder / "config.toml"
    hooks_path = folder / "hooks.toml"
    hook_owner = partial(
        owned, client=HARNESS, python_executable=context.python
    )
    validate_project_paths((config_path, hooks_path), context.project_dir)
    content, found, original = merged_toml(
        config_path, _owns_entry, _config(neutral, context, config_path)
    )
    files[config_path] = content
    conflicts += found
    _keep_backup(files, config_path, original)
    content, found, original = merged_toml(
        hooks_path, hook_owner, {"hooks": _hooks(context)}
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
    disabled_tools = disabled_tools_for_role(context.role)
    config: dict[str, Any] = {
        "active_model": context.model or "",
        "disabled_tools": list(disabled_tools),
        "enable_subagents": False,
        "enable_background_processes": False,
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
    config: dict = {"mcp_servers": _servers(neutral, context)}
    if context.model:
        config["active_model"] = context.model
    # Lists merge by appending, so only add the names the member's file lacks.
    present = _existing_disabled_tools(path)
    native_names = {_TOOL_NAMES.get(name, name) for name in neutral.deny_tools}
    disabled = sorted(native_names - set(present))
    if disabled:
        config["disabled_tools"] = disabled
    return config


def _existing_disabled_tools(path: Path) -> list[str]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8")).get("disabled_tools", [])
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return value if isinstance(value, list) else []


def _servers(neutral: NeutralConfig, context: LaunchContext) -> list[dict]:
    servers = []
    for server in active_servers(neutral):
        if server.kind == "remote":
            entry = {"name": server.name, "transport": "http", "url": server.url}
            route = context.memory_service or {}
            authorization = route.get("authorization_env")
            if server.name == "compchem-memory" and isinstance(authorization, str):
                entry["auth"] = {
                    "type": "static",
                    "api_key_env": authorization,
                }
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
