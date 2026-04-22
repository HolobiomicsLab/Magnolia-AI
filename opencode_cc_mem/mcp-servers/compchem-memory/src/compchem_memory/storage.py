"""Storage resolution: project-local memory store at project_dir/.magnolia/."""

import json
from pathlib import Path

GLOBAL_BASE = Path.home() / ".magnolia"
PROJECTS_DIR = GLOBAL_BASE / "projects"
SKILLS_DIR = GLOBAL_BASE / "skills"


def ensure_project_store(project_dir: str) -> Path:
    """Create and return the project-local memory directory.

    Memory is stored directly under project_dir/.magnolia/ so it travels
    with the project (git, rsync, HPC sync). A symlink from the legacy
    global hash location is maintained for backward compatibility.
    """
    resolved = Path(project_dir).resolve()
    local_dir = resolved / ".magnolia"
    local_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["entries", "runs", "sessions", "staging", "session-notes", "queue", "archive"]:
        (local_dir / sub).mkdir(parents=True, exist_ok=True)
    return local_dir


def resolve_project_dir(project_dir: str | None, default: str = ".") -> str:
    pd = project_dir or default
    return str(Path(pd).resolve())


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
