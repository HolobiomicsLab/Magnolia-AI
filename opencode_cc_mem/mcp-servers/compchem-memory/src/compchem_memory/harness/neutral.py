"""Read the OpenCode template as a harness-neutral server description."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class McpServer:
    """One MCP server, independent of a client's configuration syntax."""

    name: str
    kind: Literal["local", "remote"]
    command: list[str] | None
    url: str | None
    env: dict[str, str]
    timeout_ms: int
    enabled: bool


@dataclass
class NeutralConfig:
    """The template's MCP servers and denied tools."""

    servers: list[McpServer]
    deny_tools: frozenset[str]


def bind_memory_service(
    neutral: NeutralConfig, route: dict[str, str]
) -> NeutralConfig:
    """Replace only compchem-memory with its project-bound HTTP route."""
    servers = []
    for server in neutral.servers:
        if server.name != "compchem-memory":
            servers.append(server)
            continue
        servers.append(
            McpServer(
                name=server.name,
                kind="remote",
                command=None,
                url=route["url"],
                env={},
                timeout_ms=server.timeout_ms,
                enabled=server.enabled,
            )
        )
    return NeutralConfig(servers, neutral.deny_tools)


def load(template: Path, subs: dict[str, str], setup: dict) -> NeutralConfig:
    """Load a JSON template after applying the launcher's substitutions."""
    text = template.read_text(encoding="utf-8")
    substitutions = dict(subs)
    substitutions.setdefault(
        "@@MEMORY_ENABLED@@", "true" if _memory_enabled(setup) else "false"
    )
    text = _substitute(text, substitutions)
    rendered = json.loads(text)
    servers = [
        _server(name, value, setup, _memory_enabled(setup))
        for name, value in rendered.get("mcp", {}).items()
    ]
    tools = rendered.get("tools", {})
    denied = frozenset(name for name, enabled in tools.items() if enabled is False)
    return NeutralConfig(servers, denied)


def _substitute(text: str, substitutions: dict[str, str]) -> str:
    """Escape substitutions in JSON strings; retain raw numeric/boolean tokens."""
    pieces = []
    in_string = False
    escaped = False
    start = 0
    for index, character in enumerate(text):
        if escaped:
            escaped = False
        elif character == "\\" and in_string:
            escaped = True
        elif character == '"':
            pieces.append((start, index, in_string))
            start = index
            in_string = not in_string
    pieces.append((start, len(text), in_string))
    rendered = []
    for begin, end, string in pieces:
        piece = text[begin:end]
        for placeholder, value in substitutions.items():
            replacement = json.dumps(value, ensure_ascii=False)[1:-1] if string else value
            piece = piece.replace(placeholder, replacement)
        rendered.append(piece)
    return "".join(rendered)


def _memory_enabled(setup: dict) -> bool:
    return bool(setup.get("memory_enabled")) and bool(setup.get("memory_model"))


def _server(name: str, value: dict, setup: dict, memory_enabled: bool) -> McpServer:
    env = dict(value.get("environment", {}))
    enabled = value.get("enabled", True)
    if name == "compchem-memory":
        enabled = memory_enabled
        if memory_enabled:
            env["MAGNOLIA_MEMORY_MODEL"] = setup["memory_model"]
            if setup.get("memory_provider"):
                env["MAGNOLIA_MEMORY_PROVIDER"] = setup["memory_provider"]
            if setup.get("critic_provider"):
                env["MAGNOLIA_CRITIC_PROVIDER"] = setup["critic_provider"]
            if setup.get("critic_model"):
                env["MAGNOLIA_CRITIC_MODEL"] = setup["critic_model"]
            if setup.get("direct_fallback_provider"):
                env["MAGNOLIA_DIRECT_FALLBACK_PROVIDER"] = setup["direct_fallback_provider"]
            if setup.get("direct_fallback_model"):
                env["MAGNOLIA_DIRECT_FALLBACK_MODEL"] = setup["direct_fallback_model"]
    return McpServer(
        name=name,
        kind=value["type"],
        command=value.get("command"),
        url=value.get("url"),
        env=env,
        timeout_ms=value.get("timeout", 0),
        enabled=enabled,
    )
