"""Render a Codex launch: project ``.codex`` config and hooks, merged with the member's."""

from __future__ import annotations

from pathlib import Path
import json
from collections.abc import Mapping
from typing import Any

from compchem_memory.harness.files import (
    merged_json,
    merged_toml,
    toml_bytes,
    validate_project_paths,
)
from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.ownership import owned, strip_owned_hooks
from compchem_memory.harness.protocol import Launch, LaunchContext
from compchem_memory.harness.render_common import (
    active_servers,
    delivered_rule_text,
    hook_command,
    hook_timeout,
    policy_files,
    split_command,
    skill_links,
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
# synchronously": it is not rendered. Codex discovers project skills in
# ``.agents/skills``; Vibe shares the same links.
SKILLS_SUBDIRECTORY = ".agents/skills"
_STDIO_FIELDS = frozenset({"command", "args", "env", "env_vars"})
_HTTP_FIELDS = frozenset({
    "url", "bearer_token_env_var", "http_headers", "env_http_headers",
})
_DEVELOPER_MARKER = "<!-- magnolia-rules -->"
_MEMORY_ENV_VARS = [
    "MAGNOLIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY",
    "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "ZHIPU_API_KEY", "ZAI_API_KEY",
    "MAGNOLIA_MEMORY_PROVIDER", "MAGNOLIA_MEMORY_MODEL",
    "MAGNOLIA_LLM_PROVIDER", "MAGNOLIA_LLM_MODEL",
    "MAGNOLIA_OPENROUTER_PROVIDER", "MAGNOLIA_CRITIC",
    "MAGNOLIA_CRITIC_PROVIDER", "MAGNOLIA_CRITIC_MODEL",
    "MAGNOLIA_CRITIC_API_KEY", "MAGNOLIA_CRITIC_BASE_URL",
    "DEEPSEEK_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
    "KIMI_BASE_URL", "OPENROUTER_BASE_URL", "MISTRAL_BASE_URL", "ZAI_BASE_URL",
]


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
    magnolia = {"mcp_servers": servers}
    developer = _developer_instructions(context.root)
    member_developer = _member_developer_instructions(config_path)
    if member_developer is None or member_developer.startswith(_DEVELOPER_MARKER):
        magnolia["developer_instructions"] = developer
    else:
        conflicts.append(
            "configuration conflict: developer_instructions (member entry preserved)"
        )
    content, found, original = merged_toml(
        config_path,
        owned,
        magnolia,
        lambda current: _reconcile_transport_fields(current, servers),
    )
    files[config_path] = content
    conflicts += found
    _keep_backup(files, config_path, original)
    argv = ["codex", "-C", str(context.project_dir), "-s", "workspace-write"]
    if context.model:
        argv += ["-m", context.model]
    return Launch(
        argv=argv, env=env, files=files, conflicts=conflicts,
        links=skill_links(context, SKILLS_SUBDIRECTORY),
    )


render.binary = "codex"


def native_artifacts(
    context: LaunchContext,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, bytes]:
    """Render an immutable, client-native Codex project configuration.

    Helper/evaluator runs receive no MCP servers. Governed worker/reviewer runs
    receive only the immutable run-owned servers supplied by launch preparation.
    """
    config: dict[str, Any] = {"mcp_servers": dict(mcp_servers or {})}
    if context.model:
        config["model"] = context.model
    return {
        ".codex/config.toml": toml_bytes(config),
        ".codex/hooks.json": (
            json.dumps({"hooks": _hooks(context)}, indent=2, ensure_ascii=False)
            + "\n"
        ).encode("utf-8"),
    }


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
        obsolete = _STDIO_FIELDS if "url" in replacement else _HTTP_FIELDS | {
            field for field in ("env", "env_vars") if field not in replacement
        }
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
            if server.name == "compchem-memory":
                entry["env_vars"] = list(_MEMORY_ENV_VARS)
        entry["tool_timeout_sec"] = timeout_seconds(server)
        servers[server.name] = entry
    return servers


def _member_developer_instructions(path: Path) -> str | None:
    """Read the existing scalar without treating a member value as ours."""
    if not path.exists():
        return None
    import tomllib

    value = tomllib.loads(path.read_text(encoding="utf-8")).get("developer_instructions")
    return value if isinstance(value, str) else None


def _developer_instructions(root: Path) -> str:
    """Return Magnolia-owned static rules; Codex reads AGENTS.md natively."""
    sections = [_DEVELOPER_MARKER]
    rules = root / "rules"
    if rules.is_dir():
        for path in sorted(rules.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                sections.append(
                    f"--- rules/{path.name} ---\n{delivered_rule_text(text, HARNESS)}"
                )
    return "\n\n".join(sections)
