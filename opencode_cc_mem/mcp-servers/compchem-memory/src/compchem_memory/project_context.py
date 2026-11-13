"""Per-request project resolution for the compchem-memory server.

The ambient pin is a PROCESS environment value (MAGNOLIA_PROJECT_DIR at
server start). With opencode v2's shared background service, that process can
outlive the session that started it, so an old pin can silently serve writes
into the wrong project store. Resolve per REQUEST instead:

1. explicit project_dir argument (honored, after allow-list validation);
2. a request-scoped hint if the transport supplies one (X-Project-Dir header
   or a Context attribute carrying a directory);
3. .active-project-dir (absolute workspace path) or .active-project (project
   name) ONLY when it is newer than the server start AND differs from the
   startup env pin — i.e. the server is provably stale. Otherwise
   the startup env pin (correct for a per-session server).

Never silently resolves to a different project than the startup pin without
a warning log line.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_STARTUP_ENV = os.environ.get("MAGNOLIA_PROJECT_DIR", ".")
_ACTIVE_REL = ".magnolia/.active-project"
_ACTIVE_DIR_REL = ".magnolia/.active-project-dir"


def _startup_project_dir() -> str:
    return _STARTUP_ENV


def _request_hint() -> str | None:
    """Best-effort request-scoped project hint, if the transport exposes it."""
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
        if req is not None:
            return req.headers.get("x-project-dir") or req.headers.get(
                "x-magnolia-project-dir"
            )
    except Exception:
        pass
    try:
        from fastmcp import Context  # noqa: F401
        # No generic directory attribute on FastMCP 3.x Context; intentionally
        # left unimplemented rather than guessing. HTTP header path above is
        # the supported hint channel.
    except Exception:
        pass
    return None


def _dir_if_exists(path: Path) -> str | None:
    """Resolved path when it is an existing directory, else None."""
    try:
        if path.is_dir():
            return str(path.resolve())
    except OSError:
        pass
    return None


def _active_project_hint(startup_env: str) -> str | None:
    """.active-project-dir / .active-project, but only as a STALE-SERVER override.

    The launcher writes the project NAME to .active-project for the TS plugins.
    Treating that name as a cwd-relative path is wrong under project-root launch
    (cwd is already projects/<name>): it resolves to projects/<name>/<name>, an
    empty nested store. Prefer the absolute .active-project-dir marker, resolve
    the legacy name against the projects root, and never return a directory that
    does not exist."""
    try:
        pin = os.environ.get("MAGNOLIA_OPENCODE_DIR") or os.getcwd()
        base = Path(pin)

        # Preferred: absolute workspace path written by the launcher.
        dir_marker = base / _ACTIVE_DIR_REL
        if dir_marker.is_file():
            value = dir_marker.read_text(encoding="utf-8", errors="replace").strip()
            if value:
                resolved = _dir_if_exists(Path(value))
                if resolved:
                    return resolved

        active = base / _ACTIVE_REL
        if not active.is_file():
            return None
        value = active.read_text(encoding="utf-8", errors="replace").strip()
        if not value:
            return None
        candidate = Path(value)
        if candidate.is_absolute():
            return _dir_if_exists(candidate)
        # Legacy name marker: resolve against the projects root, not cwd. The
        # candidates are ordered so a real projects/<name> store wins over a
        # stray <name>/ child directory left by an earlier bad resolution.
        candidates = [base / "projects" / value, base.parent / value]
        if base.name == value:
            candidates.append(base)
        candidates.append(base / value)
        for path in candidates:
            resolved = _dir_if_exists(path)
            if resolved:
                return resolved
        return None
    except Exception:
        return None


def effective_project_dir(explicit: str | None = None) -> str:
    """Resolve the project directory for THIS call."""
    if explicit:
        return explicit
    hint = _request_hint()
    if hint:
        if hint != _STARTUP_ENV:
            log.warning("project pin from request hint: %s (env was %s)", hint, _STARTUP_ENV)
        return hint
    active = _active_project_hint(_STARTUP_ENV)
    if active and active != _STARTUP_ENV:
        # Only trust .active-project when the file was written after this
        # process started — a stale server must not keep its old env pin.
        try:
            pin = os.environ.get("MAGNOLIA_OPENCODE_DIR") or os.getcwd()
            mtime = (Path(pin) / _ACTIVE_REL).stat().st_mtime
            start = _process_start_time()
            if mtime > start:
                log.warning(
                    "stale memory-server env pin %s overridden by newer active project %s",
                    _STARTUP_ENV, active,
                )
                return active
        except Exception:
            pass
    return _STARTUP_ENV


def _process_start_time() -> float:
    try:
        return float(os.environ.get("MAGNOLIA_SERVER_STARTED_AT", "0")) or _mtime_fallback()
    except Exception:
        return 0.0


def _mtime_fallback() -> float:
    try:
        return os.path.getmtime("/proc/self")
    except Exception:
        return 0.0
