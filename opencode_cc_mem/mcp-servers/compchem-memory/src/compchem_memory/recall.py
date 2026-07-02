"""Tool-scoped warning recall — the entry point the submit_job recall gate imports.

Kept separate from server.py so importers (compchem_tools) don't pull in the
whole FastMCP server. Returns plain Python lists, not JSON strings.
"""
import os
from pathlib import Path
from typing import Any

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.storage import resolve_project_dir, ensure_project_store

GLOBAL_BASE = Path(os.path.expanduser("~/.magnolia"))


def warnings_for_tool(
    tool: str, project_dir: str | None = None, limit: int = 5,
) -> list[dict[str, Any]]:
    """Resolve the project store + ProjectManager, then delegate to
    ProjectManager.select_warnings_for_tool. Returns [] on falsy tool."""
    if not tool:
        return []
    pd = resolve_project_dir(project_dir, os.environ.get("MAGNOLIA_PROJECT_DIR", "."))
    ensure_project_store(pd)
    mgr = ProjectManager(GLOBAL_BASE)
    return mgr.select_warnings_for_tool(pd, tool, limit=limit)


def similar_runs(
    tool: str, system_tags: list[str], project_dir: str | None = None, limit: int = 3,
) -> list[dict[str, Any]]:
    """Resolve store + manager, delegate to ProjectManager.similar_runs.
    Returns [] on falsy tool/tags."""
    if not tool or not system_tags:
        return []
    pd = resolve_project_dir(project_dir, os.environ.get("MAGNOLIA_PROJECT_DIR", "."))
    ensure_project_store(pd)
    return ProjectManager(GLOBAL_BASE).similar_runs(pd, tool, system_tags, limit=limit)
