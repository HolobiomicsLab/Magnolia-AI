"""Shared capture infrastructure: per-project SessionManager registry + decorator (Task 2)."""

import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

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


def _summarize_args(args: tuple, kwargs: dict) -> str:
    parts = []
    for a in args[:3]:
        parts.append(str(a)[:80])
    for k, v in list(kwargs.items())[:5]:
        if k == "project_dir":
            continue
        parts.append(f"{k}={str(v)[:80]}")
    return ", ".join(parts)


def _summarize_result(result: Any) -> str:
    if result is None:
        return "None"
    s = str(result)
    return s[:200] + ("..." if len(s) > 200 else "")


def captured(source: str):
    """Decorator: log tool_call + tool_success/tool_error around an MCP tool.

    The wrapped function MUST accept `project_dir` as a keyword argument (existing
    MCP tools already follow this convention). Logging failures never propagate.
    """
    def decorator(fn: Callable) -> Callable:
        tool_name = fn.__name__

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            project_dir = kwargs.get("project_dir") or "."
            mgr = None
            try:
                mgr = get_session_manager(project_dir)
                mgr.record("tool_call", {
                    "source": source,
                    "tool": tool_name,
                    "args_summary": _summarize_args(args, kwargs),
                })
            except Exception:
                pass

            t0 = time.time()
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                duration_ms = int((time.time() - t0) * 1000)
                try:
                    if mgr is not None:
                        mgr.record("tool_error", {
                            "source": source,
                            "tool": tool_name,
                            "duration_ms": duration_ms,
                            "error": f"{type(e).__name__}: {str(e)[:500]}",
                        })
                except Exception:
                    pass
                raise

            duration_ms = int((time.time() - t0) * 1000)
            try:
                if mgr is not None:
                    mgr.record("tool_success", {
                        "source": source,
                        "tool": tool_name,
                        "duration_ms": duration_ms,
                        "result_summary": _summarize_result(result),
                    })
            except Exception:
                pass
            return result

        return wrapper

    return decorator
