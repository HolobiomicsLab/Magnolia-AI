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


def build_boot_payload(paths: ProjectPaths) -> str:
    """Return the plugin-compatible boot payload, truncated to a 256k prefix."""
    parts = [
        f"<!-- magnolia-boot-context:{paths.name} -->",
        f"[MAGNOLIA BOOT CONTEXT — project {paths.name}]",
    ]
    for name in ("boot-context.md", "audit-report.md"):
        section = _section(name, paths.store / name)
        if section is not None:
            parts.append(section)
    parts.extend(_rule_sections(paths))
    payload = "\n\n".join(parts)
    encoded = payload.encode("utf-16-le")
    if len(encoded) > 2 * MAX_PAYLOAD_CHARS:
        return encoded[: 2 * MAX_PAYLOAD_CHARS].decode("utf-16-le", errors="ignore") + _TRUNCATION_SUFFIX
    return payload
