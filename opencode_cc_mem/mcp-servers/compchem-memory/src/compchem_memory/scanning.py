"""Fast header scanning: read only YAML frontmatter from memory entries."""

from pathlib import Path
from typing import Any

import yaml


def scan_memory_headers(directory: Path, max_files: int = 200) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    results = []
    md_files = sorted(
        directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for f in md_files:
        if f.name == "INDEX.md":
            continue
        if len(results) >= max_files:
            break
        meta = _read_frontmatter_only(f)
        if meta is None:
            continue
        results.append(
            {
                "filename": f.name,
                "path": str(f),
                "title": meta.get("title", f.stem),
                "type": meta.get("type", "note"),
                "description": meta.get("description", ""),
                "tools": meta.get("tools", []),
                "tags": meta.get("tags", []),
                "confidence": meta.get("confidence", 0.5),
                "observation_count": meta.get("observation_count", 0),
                "last_verified": meta.get("last_verified", ""),
                "date": meta.get("date", meta.get("created", "")),
            }
        )
    return results


def scan_skills_headers(skills_dir: Path, max_files: int = 50) -> list[dict[str, Any]]:
    if not skills_dir.exists():
        return []
    results = []
    md_files = sorted(
        skills_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for f in md_files:
        if len(results) >= max_files:
            break
        meta = _read_frontmatter_only(f)
        if meta is None:
            continue
        results.append(
            {
                "filename": f.name,
                "path": str(f),
                "tool": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "version": meta.get("version", ""),
                "tags": meta.get("tags", []),
                "last_verified": meta.get("last_verified", ""),
            }
        )
    return results


def format_manifest(headers: list[dict[str, Any]]) -> str:
    lines = []
    for i, h in enumerate(headers, 1):
        title = h.get("title", h.get("tool", "unknown"))
        entry_type = h.get("type", "skill")
        tags = ", ".join(h.get("tags", []))
        desc = h.get("description", "")
        line = f"{i}. [{entry_type}] {title} ({h['filename']})"
        lines.append(line)
        if desc:
            lines.append(f"   Description: {desc}")
        if tags:
            lines.append(f"   Tags: {tags}")
    return "\n".join(lines)


def _read_frontmatter_only(f: Path) -> dict[str, Any] | None:
    try:
        with open(f, "r") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return {}
            parts = []
            for line in fh:
                if line.strip() == "---":
                    break
                parts.append(line)
            fm = "".join(parts).strip()
            if not fm:
                return {}
            return yaml.safe_load(fm) or {}
    except (yaml.YAMLError, OSError):
        return None
