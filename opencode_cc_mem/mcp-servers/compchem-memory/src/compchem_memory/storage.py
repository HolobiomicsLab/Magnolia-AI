"""Storage resolution: project-local memory store at project_dir/.magnolia/."""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

GLOBAL_BASE = Path.home() / ".magnolia"
PROJECTS_DIR = GLOBAL_BASE / "projects"
# Elevated rules live in the repo's git-tracked rules/ directory (loaded every
# session as doctrine); this global fallback only applies when the server runs
# outside an opencode workspace that sets MAGNOLIA_RULES_DIR. The skill tier
# (formerly ~/.magnolia/skills) was retired 2026-09: protocols moved to
# .opencode/skills/, learnings stay in the memory tiers.
RULES_DIR = GLOBAL_BASE / "rules"


def resolved_rules_dir() -> Path:
    """The effective rules directory: the MAGNOLIA_RULES_DIR workspace override
    when set, else the global fallback RULES_DIR. Single source of truth shared
    by server.py and startup_scan.py — startup_scan must not import server for
    this value (that circular import re-executed server.py's module body and
    spawned a second boot pipeline, 2026-09-17)."""
    return Path(os.environ.get("MAGNOLIA_RULES_DIR", str(RULES_DIR)))


def ensure_project_store(project_dir: str) -> Path:
    """Create and return the project-local memory directory.

    Memory is stored directly under project_dir/.magnolia/ so it travels
    with the project (git, rsync, HPC sync). A symlink from the legacy
    global hash location is maintained for backward compatibility.
    """
    resolved = Path(project_dir).resolve()
    local_dir = resolved / ".magnolia"
    local_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["entries", "runs", "sessions", "staging", "session-notes", "queue", "archive", "backups"]:
        (local_dir / sub).mkdir(parents=True, exist_ok=True)
    return local_dir


def resolve_project_dir(project_dir: str | None, default: str = ".") -> str:
    pd = project_dir or default
    return str(Path(pd).resolve())


def backup_file(src: Path, project_dir: str) -> Path | None:
    """Copy a file to .magnolia/backups/ before destructive mutation.

    Returns the backup path, or None if the source doesn't exist.
    Backup filename: {original_stem}_{timestamp}.md
    """
    if not src.exists():
        return None
    store = Path(project_dir) / ".magnolia"
    if store.is_symlink():
        store = store.resolve()
    backups_dir = store / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"{src.stem}_{ts}{src.suffix}"
    dest = backups_dir / backup_name
    shutil.copy2(src, dest)
    return dest


def scaffold_obsidian_vault(project_dir: str) -> Path:
    """Create .obsidian/ directory with vault configuration.

    Creates app.json (vault settings with wikilinks), appearance.json,
    and a daily-note template. Only called via `magnolia-memory init-vault`.
    Does NOT modify .magnolia/ or any existing files.
    """
    resolved = Path(project_dir).resolve()
    obsidian_dir = resolved / ".obsidian"
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    (obsidian_dir / "templates").mkdir(parents=True, exist_ok=True)

    app_config = {
        "attachmentFolderPath": ".magnolia/entries",
        "newFileLocation": "folder",
        "newFileFolderPath": ".magnolia/entries",
        "useMarkdownLinks": False,
        "showUnsupportedFiles": True,
        "promptDelete": False,
    }
    (obsidian_dir / "app.json").write_text(json.dumps(app_config, indent=2) + "\n")

    appearance = {"cssTheme": "", "enabledCssSnippets": []}
    (obsidian_dir / "appearance.json").write_text(
        json.dumps(appearance, indent=2) + "\n"
    )

    template_content = _get_daily_note_template()
    (obsidian_dir / "templates" / "daily-note.md").write_text(template_content)

    return obsidian_dir


def _get_daily_note_template() -> str:
    """Return Obsidian daily note template."""
    return """---
type: daily_note
date: "{{date}}"
tags: [daily-note, lab-notebook]
---

# Lab Notebook — {{date}}

## Session Activity
<!-- Auto-populated by: magnolia-memory generate-daily-note {{date}} -->

## Entries Created
<!-- Wikilinks to entries created today will appear here -->

## Runs
<!-- Run records from today -->

## Notes
<!-- Human annotations and observations -->
"""
