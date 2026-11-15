"""Render a Codex launch: project ``.codex`` config and hooks, merged with the member's."""

from __future__ import annotations

from pathlib import Path

from compchem_memory.harness.files import (
    merged_json,
    merged_toml,
    validate_project_paths,
)
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
_STDIO_FIELDS = frozenset({"command", "args", "env"})
_HTTP_FIELDS = frozenset({
    "url", "bearer_token_env_var", "http_headers", "env_http_headers",
})


def render(neutral: NeutralConfig, context: LaunchContext) -> Launch:
    files, env = policy_files(neutral, context, HARNESS)
    folder = context.project_dir / ".codex"
    hooks_path = folder / "hooks.json"
    config_path = folder / "config.toml"
    validate_project_paths((hooks_path, config_path), context.project_dir)
    conflicts: list[str] = []
    content, found = merged_json(
        hooks_path, owned, {"hooks": _hooks(context)}, strip_owned_hooks
    )
    files[hooks_path] = content
    conflicts += found
    servers = _servers(neutral)
    content, found, original = merged_toml(
        config_path,
        owned,
        {"mcp_servers": servers},
        lambda current: _reconcile_transport_fields(current, servers),
    )
    files[config_path] = content
    conflicts += found
    _keep_backup(files, config_path, original)
    argv = ["codex", "-C", str(context.project_dir), "-s", "workspace-write"]
    if context.model:
        argv += ["-m", context.model]
    return Launch(argv=argv, env=env, files=files, conflicts=conflicts)


render.binary = "codex"


def _keep_backup(files: dict[Path, bytes], path: Path, original: bytes | None) -> None:
    backup = path.with_name(path.name + ".magnolia-backup")
    if original is not None and not backup.exists():
        files[backup] = original


def _reconcile_transport_fields(current: dict, rendered: dict) -> dict:
    """Remove obsolete transport keys from renderer-owned Codex servers."""
    existing = current.get("mcp_servers")
    if not isinstance(existing, dict):
        return current
    servers = dict(existing)
    for name, replacement in rendered.items():
        entry = servers.get(name)
        if not isinstance(entry, dict) or not owned("server", name, entry):
            continue
        obsolete = _STDIO_FIELDS if "url" in replacement else _HTTP_FIELDS
        servers[name] = {key: value for key, value in entry.items() if key not in obsolete}
    return {**current, "mcp_servers": servers}


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
