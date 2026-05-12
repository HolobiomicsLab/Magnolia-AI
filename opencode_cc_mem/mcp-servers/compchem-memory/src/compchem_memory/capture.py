"""Shared capture infrastructure: per-project SessionManager registry + decorator (Task 2)."""

from pathlib import Path

from compchem_memory.tiers.session import SessionManager

_session_managers: dict[str, SessionManager] = {}


def get_session_manager(project_dir: str, project_id: str | None = None) -> SessionManager:
    """Return the SessionManager for this project_dir, creating if needed.
    Different project_dirs get different managers — never replaced."""
    key = str(Path(project_dir).resolve())
    if key not in _session_managers:
        sessions_dir = Path(key) / ".magnolia" / "sessions"
        pid = project_id or Path(key).name
        _session_managers[key] = SessionManager(
            sessions_dir, project_id=pid, project_dir=key
        )
    return _session_managers[key]


def reset_registry() -> None:
    """Test helper: clear the registry."""
    _session_managers.clear()
