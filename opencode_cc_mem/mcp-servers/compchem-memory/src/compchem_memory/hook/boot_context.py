"""Build the project boot-context payload for a harness model request.

Deliberate change: a high surrogate left alone at the UTF-16 truncation cut is dropped.
"""

from pathlib import Path

from compchem_memory.hook.project import ProjectPaths

MAX_PAYLOAD_CHARS = 256_000
_TRUNCATION_SUFFIX = "\n\n[truncated at 256k chars]"


def _section(name: str, path: Path) -> str | None:
    try:
        text = path.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    return f"--- {name} ---\n{text}" if text else None


def _rule_sections(paths: ProjectPaths) -> list[str]:
    try:
        names = sorted(path.name for path in (paths.root / "rules").iterdir() if path.name.endswith(".md"))
    except OSError:
        return []
    sections = (_section(f"rules/{name}", paths.root / "rules" / name) for name in names)
    return [section for section in sections if section is not None]


def _dynamic_sections(paths: ProjectPaths) -> list[str]:
    sections = (_section(name, paths.store / name) for name in ("boot-context.md", "audit-report.md"))
    return [section for section in sections if section is not None]


def _header(paths: ProjectPaths) -> list[str]:
    return [
        f"<!-- magnolia-boot-context:{paths.name} -->",
        f"[MAGNOLIA BOOT CONTEXT — project {paths.name}]",
    ]


def build_claude_boot_payload(paths: ProjectPaths, limit: int) -> str:
    """Return the header and at most ``limit`` characters of the dynamic part.

    Claude gets its rules through the system-prompt file, so the rule sections
    are left out; a dynamic part past ``limit`` ends with a pointer to the files.
    """
    dynamic = "\n\n".join(_dynamic_sections(paths))
    parts = _header(paths)
    if dynamic:
        parts.append(dynamic[:limit])
    if len(dynamic) > limit:
        parts.append(
            f"[Magnolia] The full project memory summary is in {paths.project_dir}/.magnolia/boot-context.md"
            " and audit-report.md: read both before your first task."
        )
    return "\n\n".join(parts)


def build_boot_payload(paths: ProjectPaths, include_agents_md: bool = False) -> str:
    """Return the plugin-compatible boot payload, truncated to a 256k prefix.

    ``include_agents_md`` puts the workspace ``AGENTS.md`` first, for a client
    that does not load it itself; an unreadable file leaves the payload as is.
    """
    parts = _header(paths)
    if include_agents_md:
        agents = _section("AGENTS.md", paths.root / "AGENTS.md")
        if agents is not None:
            parts.append(agents)
    parts.extend(_dynamic_sections(paths))
    parts.extend(_rule_sections(paths))
    payload = "\n\n".join(parts)
    encoded = payload.encode("utf-16-le")
    if len(encoded) > 2 * MAX_PAYLOAD_CHARS:
        return encoded[: 2 * MAX_PAYLOAD_CHARS].decode("utf-16-le", errors="ignore") + _TRUNCATION_SUFFIX
    return payload
