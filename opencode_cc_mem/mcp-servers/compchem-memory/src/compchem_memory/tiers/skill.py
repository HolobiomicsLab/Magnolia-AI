"""Skill tier: authoritative, reviewed reference for a tool or technique."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class SkillManager:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[dict[str, Any]]:
        results = []
        for f in sorted(self.skills_dir.glob("*.md")):
            meta = self._parse_frontmatter(f.read_text())
            results.append(
                {
                    "name": f.name,
                    "tool": meta.get("name", f.stem),
                    "description": meta.get("description", ""),
                    "version": meta.get("version", ""),
                    "last_verified": meta.get("last_verified", ""),
                    "path": str(f),
                }
            )
        return results

    def get_skill(self, tool_name: str) -> str | None:
        for f in self.skills_dir.glob("*.md"):
            text = f.read_text()
            meta = self._parse_frontmatter(text)
            if meta.get("name", "").lower() == tool_name.lower():
                return text
            if tool_name.lower() in f.name.lower():
                return text
        return None

    def get_skill_content(
        self, tool_name: str, section: str | None = None
    ) -> str | None:
        full = self.get_skill(tool_name)
        if full is None:
            return None
        if section is None:
            return full
        return self._extract_section(full, section)

    def search_skills(
        self, keyword: str = "", tags: list[str] | None = None
    ) -> list[dict[str, Any]]:
        results = []
        for f in self.skills_dir.glob("*.md"):
            text = f.read_text()
            meta = self._parse_frontmatter(text)
            match = True
            if keyword:
                match = keyword.lower() in text.lower()
            if match and tags:
                entry_tags = meta.get("tags", [])
                if not any(
                    t.lower() in [et.lower() for et in entry_tags] for t in tags
                ):
                    match = False
            if match:
                results.append(
                    {
                        "name": f.name,
                        "tool": meta.get("name", f.stem),
                        "tier": "skill",
                        "confidence": 1.0,
                        "path": str(f),
                    }
                )
        return results

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

    def _extract_section(self, text: str, section: str) -> str | None:
        pattern = re.compile(rf"^##+\s+{re.escape(section)}", re.MULTILINE)
        m = pattern.search(text)
        if not m:
            return None
        start = m.start()
        next_m = re.compile(r"^##+\s+", re.MULTILINE).search(text, m.end())
        end = next_m.start() if next_m else len(text)
        return text[start:end].strip()


import re
