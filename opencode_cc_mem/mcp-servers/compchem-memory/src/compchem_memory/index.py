"""Memory index: lightweight catalogue across all tiers."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class MemoryIndex:
    def __init__(self, global_base: Path):
        self.global_base = global_base

    def build_index(
        self, project_dir: str | None = None, skills_dir: str | None = None
    ) -> list[dict[str, Any]]:
        entries = []
        if skills_dir:
            skills_path = Path(skills_dir)
            if skills_path.exists():
                for f in skills_path.glob("*.md"):
                    text = f.read_text()
                    meta = self._parse_frontmatter(text)
                    entries.append(
                        {
                            "tier": "skill",
                            "name": f.name,
                            "tool": meta.get("name", f.stem),
                            "tags": meta.get("tags", []),
                            "date": meta.get("last_verified", ""),
                            "path": str(f),
                        }
                    )
        if project_dir:
            entries_dir = Path(project_dir) / ".magnolia" / "entries"
            if entries_dir.exists():
                for f in entries_dir.glob("*.md"):
                    if f.name == "INDEX.md":
                        continue
                    text = f.read_text()
                    meta = self._parse_frontmatter(text)
                    entries.append(
                        {
                            "tier": "project",
                            "name": f.name,
                            "title": meta.get("title", f.stem),
                            "tags": meta.get("tags", []),
                            "date": meta.get("date", ""),
                            "path": str(f),
                            "scope": project_dir,
                        }
                    )
        return entries

    def _parse_frontmatter(self, text: str) -> dict[str, Any]:
        if not text.startswith("---"):
            return {}
        end = text.find("---", 3)
        if end == -1:
            return {}
        try:
            return yaml.safe_load(text[3:end].strip()) or {}
        except yaml.YAMLError:
            return {}
