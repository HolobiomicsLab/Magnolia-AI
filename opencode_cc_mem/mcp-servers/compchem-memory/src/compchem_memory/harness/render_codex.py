"""Render a Codex launch: project ``.codex`` config and hooks, merged with the member's."""

from __future__ import annotations

from pathlib import Path

from compchem_memory.harness.files import merged_json, merged_toml
from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.ownership import owned, strip_owned_hooks
from compchem_memory.harness.protocol import Launch, LaunchContext
from compchem_memory.harness.render_common import (
    active_servers,
    hook_command,
    hook_timeout,
    policy_files,
    split_command,
    timeout_seconds,
)

HARNESS = "codex"
EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "prompt",
    "PreToolUse": "pre-tool",
    "PostToolUse": "post-tool",
    "Stop": "turn-end",
}
# Codex 0.154 parses an ``async`` key but logs "running async <event> hook
# synchronously": it is not rendered. Skills: no directory is named by its help or
# bundled documentation, so none are linked (None = Codex gets no skills).
SKILLS_SUBDIRECTORY: str | None = None


def render(neutral: NeutralConfig, context: LaunchContext) -> Launch:
    files, env = policy_files(neutral, context, HARNESS)
    folder = context.project_dir / ".codex"
    conflicts: list[str] = []
    content, found = merged_json(
        folder / "hooks.json", owned, {"hooks": _hooks(context)}, strip_owned_hooks
    )
    files[folder / "hooks.json"] = content
    conflicts += found
    content, found, original = merged_toml(
        folder / "config.toml", owned, {"mcp_servers": _servers(neutral)}
    )
    files[folder / "config.toml"] = content
    conflicts += found
    _keep_backup(files, folder / "config.toml", original)
    argv = ["codex", "-C", str(context.project_dir), "-s", "workspace-write"]
    if context.model:
        argv += ["-m", context.model]
    return Launch(argv=argv, env=env, files=files, conflicts=conflicts)


render.binary = "codex"


def _keep_backup(files: dict[Path, bytes], path: Path, original: bytes | None) -> None:
    backup = path.with_name(path.name + ".magnolia-backup")
    if original is not None and not backup.exists():
        files[backup] = original


def _hooks(context: LaunchContext) -> dict:
    return {
        event: [{"hooks": [{
            "type": "command",
            "command": hook_command(context, HARNESS, name),
            "timeout": hook_timeout(name),
        }]}]
        for event, name in EVENTS.items()
    }


def _servers(neutral: NeutralConfig) -> dict:
    servers = {}
    for server in active_servers(neutral):
        if server.kind == "remote":
            entry = {"url": server.url}
        else:
            command, args = split_command(server)
            entry = {"command": command, "args": args}
            if server.env:
                entry["env"] = dict(server.env)
        entry["tool_timeout_sec"] = timeout_seconds(server)
        servers[server.name] = entry
    return servers
