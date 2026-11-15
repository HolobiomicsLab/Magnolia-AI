"""Resolve a workspace project and bind it to a harness session.

Bindings use a same-directory temporary file and ``os.link`` for atomic,
exclusive creation after ``fsync``. A partial or empty read is retried for up
to 200 ms; a still-damaged binding is treated as no project.
Deliberate change: root mode uses the first ancestor holding
``mcp-servers/compchem-memory`` or ``rules``; this normalises the differing
plugin root rules and removes the ``projects/xiulian`` fallback.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from compchem_memory.hook.state import safe_component


_READ_RETRY_SECONDS = 0.2
_READ_RETRY_INTERVAL_SECONDS = 0.01


@dataclass(frozen=True)
class ProjectPaths:
    """Absolute paths identifying one project's memory store."""

    root: Path
    project_dir: Path
    store: Path
    name: str
    root_mode: bool


def state_home(env: Mapping[str, str]) -> Path:
    """Return the Magnolia state directory from the supplied environment."""
    default_home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    base = Path(env["XDG_STATE_HOME"]) if env.get("XDG_STATE_HOME") else default_home / ".local" / "state"
    home = base.absolute() / "magnolia"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


def _is_root_mode(cwd: Path, env: Mapping[str, str]) -> bool:
    return env.get("MAGNOLIA_PROJECT_WORKSPACE") == "1" or any(
        (cwd / ".magnolia" / marker).exists()
        for marker in ("GOAL.md", "boot-context.md")
    )


def _workspace_root(cwd: Path) -> Path:
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / "mcp-servers" / "compchem-memory").exists() or (ancestor / "rules").exists():
            return ancestor
    return cwd


def _project_paths(root: Path, project_dir: Path, root_mode: bool) -> ProjectPaths:
    return ProjectPaths(
        root=root,
        project_dir=project_dir,
        store=project_dir / ".magnolia",
        name=project_dir.name,
        root_mode=root_mode,
    )


def _active_project(cwd: Path) -> Path | None:
    try:
        name = (cwd / ".magnolia" / ".active-project").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return cwd / "projects" / name if name else None


def resolve_project(cwd: Path, env: Mapping[str, str]) -> ProjectPaths | None:
    """Resolve the project using root mode, active marker, then environment."""
    cwd = cwd.absolute()
    if _is_root_mode(cwd, env):
        return _project_paths(_workspace_root(cwd), cwd, True)
    project_dir = _active_project(cwd)
    if project_dir is None and env.get("MAGNOLIA_PROJECT_DIR"):
        project_dir = Path(env["MAGNOLIA_PROJECT_DIR"])
        if not project_dir.is_absolute():
            project_dir = cwd / project_dir
    if project_dir is None:
        return None
    return _project_paths(cwd, project_dir.absolute(), False)


def _serialise(project: ProjectPaths | None) -> dict[str, object]:
    if project is None:
        return {"project": None}
    return {
        "project": {
            "root": str(project.root),
            "project_dir": str(project.project_dir),
            "store": str(project.store),
            "name": project.name,
            "root_mode": project.root_mode,
        }
    }


def _deserialise(data: dict[str, object]) -> ProjectPaths | None:
    project = data["project"]
    if project is None:
        return None
    return ProjectPaths(
        root=Path(project["root"]),
        project_dir=Path(project["project_dir"]),
        store=Path(project["store"]),
        name=project["name"],
        root_mode=project["root_mode"],
    )


def _read_binding(path: Path) -> ProjectPaths | None:
    deadline = time.monotonic() + _READ_RETRY_SECONDS
    while True:
        try:
            return _deserialise(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(_READ_RETRY_INTERVAL_SECONDS)


def _create_binding(path: Path, project: ProjectPaths | None) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".binding-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(_serialise(project), temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_name, path)
    finally:
        os.unlink(temporary_name)


def bind_project(
    harness: str,
    session_id: str,
    cwd: Path,
    env: Mapping[str, str],
) -> ProjectPaths | None:
    """Return the session's first binding, creating it on any first event."""
    bindings = state_home(env) / "bindings"
    bindings.mkdir(mode=0o700, exist_ok=True)
    filename = f"{safe_component(harness)}-{safe_component(session_id)}.json"
    binding_path = bindings / filename
    try:
        _create_binding(binding_path, resolve_project(cwd, env))
    except FileExistsError:
        pass
    return _read_binding(binding_path)
