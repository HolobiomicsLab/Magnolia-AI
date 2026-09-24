"""Shared capture infrastructure: per-project SessionManager registry + decorator (Task 2).

Capture versions (P1/A1 — the receipts extractor treats the stamp as
load-bearing and degrades gracefully on unstamped/v1 records):

  v2 (full fidelity, allowlisted tools — see capture_manifest): every kwarg
    captured structured under ``args``; the full result under
    ``result_summary`` plus a structured ``result`` twin when it is JSON.
  v1 (everything else): legacy summary — 3 positional args, 5 kwargs, values
    @80 chars; result @200 chars. Historical v1 records truncate SILENTLY
    (no stamp); new v1 records carry the stamp and flag every cut.

The historical bug was the SILENCE of truncation, not the number — every cut
today (including v2's generous safety caps) is flagged on the record.
"""

import json
import os
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from compchem_memory.tiers.session import SessionManager
from compchem_memory.capture_manifest import FULL_FIDELITY_TOOLS
from compchem_memory import distill_log
from compchem_memory.storage import resolve_project_dir

CAPTURE_VERSION_LEGACY = 1
CAPTURE_VERSION_FULL = 2

# Safety valves for v2 — far above any real orchestration payload, but a
# runaway string must never bloat the session log. Anything cut here is
# flagged (args_truncated / result_truncated), never silent.
V2_ARGS_CAP = 64_000
V2_RESULT_CAP = 64_000

_session_managers: dict[str, SessionManager] = {}


def _resolve_project_dir(project_dir: str | None) -> str:
    """Resolve the project dir the same way the tools do: explicit kwarg, else
    the server's pinned MAGNOLIA_PROJECT_DIR, else cwd.

    History: the decorator used the raw kwarg with a "." fallback, so tools
    called without project_dir (the normal case from opencode) logged their
    session events to a stray store at the server's cwd (repo root) and drained
    .distill-notices from that wrong store — notices pushed to the real project
    store were never delivered (1003 piled up undrained over 7 weeks)."""
    return resolve_project_dir(project_dir, os.environ.get("MAGNOLIA_PROJECT_DIR", "."))


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


def _jsonable(value: Any) -> Any:
    """Best-effort JSON value for the structured ``args``/``result`` fields."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _call_fields(full_fidelity: bool, args: tuple, kwargs: dict) -> dict[str, Any]:
    """tool_call payload fields, by capture version.

    v2: all kwargs structured under ``args`` (plus the string ``args_summary``
    legacy consumers read). If the serialized blob exceeds the safety cap the
    structured fields are omitted — the flag says why.
    v1: legacy summary. Both versions set ``args_truncated`` whenever anything
    was cut, so no truncation is silent anymore.
    """
    if full_fidelity:
        positional = [str(a) for a in args]
        full_kwargs = {k: _jsonable(v) for k, v in kwargs.items()}
        blob = json.dumps(
            {"positional": positional, "kwargs": full_kwargs}, default=str
        )
        truncated = len(blob) > V2_ARGS_CAP
        fields: dict[str, Any] = {
            "capture_version": CAPTURE_VERSION_FULL,
            "args_truncated": truncated,
        }
        if not truncated:
            fields["args_positional"] = positional
            fields["args"] = full_kwargs
        summary = ", ".join(
            positional + [f"{k}={v}" for k, v in kwargs.items()]
        )
        fields["args_summary"] = (
            summary[:V2_ARGS_CAP] if len(summary) > V2_ARGS_CAP else summary
        )
        return fields
    parts = []
    truncated = len(args) > 3
    for a in args[:3]:
        sa = str(a)
        if len(sa) > 80:
            truncated = True
        parts.append(sa[:80])
    items = list(kwargs.items())
    if len(items) > 5:
        truncated = True
    for k, v in items[:5]:
        if k == "project_dir":
            continue
        sv = str(v)
        if len(sv) > 80:
            truncated = True
        parts.append(f"{k}={sv[:80]}")
    return {
        "capture_version": CAPTURE_VERSION_LEGACY,
        "args_summary": ", ".join(parts),
        "args_truncated": truncated,
    }


def _result_fields(full_fidelity: bool, result: Any) -> dict[str, Any]:
    """tool_success payload fields, by capture version.

    v2: the full result string, plus a structured ``result`` twin when it
    parses as JSON and fits the safety cap. v1: the legacy 200-char summary.
    ``result_truncated`` flags every cut in both versions.
    """
    if result is None:
        return {"result_summary": "None", "result_truncated": False}
    if full_fidelity:
        # compchem-memory tools return JSON strings; compchem-tools tools
        # return dicts. Both become canonical JSON + a structured twin.
        if isinstance(result, (dict, list)):
            s = json.dumps(result, default=str)
            parsed = result
        else:
            s = str(result)
            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                parsed = None
        fields: dict[str, Any] = {"capture_version": CAPTURE_VERSION_FULL}
        if isinstance(parsed, (dict, list)) and len(s) <= V2_RESULT_CAP:
            fields["result"] = parsed
        truncated = len(s) > V2_RESULT_CAP
        fields["result_summary"] = s[:V2_RESULT_CAP] if truncated else s
        fields["result_truncated"] = truncated
        return fields
    s = str(result)
    truncated = len(s) > 200
    return {
        "capture_version": CAPTURE_VERSION_LEGACY,
        "result_summary": s[:200] + ("..." if truncated else ""),
        "result_truncated": truncated,
    }


def _attach_distill_notices(result: Any, project_dir: str) -> Any:
    """Drain the .distill-notices queue and attach notices to a tool's result so
    background/inline distillations surface in the dialogue. Never raises.

    Most @captured tools return ``json.dumps(dict)`` — a JSON *string*. The
    str path historically appended the notice text to that string, which
    CORRUPTED the JSON (the gateway's _tool_result then falls back to a
    ``{_text}`` preview and every structured consumer — the web workbench's
    show_structure/submit_job/scores projections — silently misses). So for
    JSON-object strings, re-serialize with a ``_distill_notices`` key instead
    (the same shape the dict path has always produced). Plain-text strings
    keep the old concatenated form; the agent model reads both fine."""
    try:
        notices = distill_log.drain_distill_notices(project_dir)
        if not notices:
            return result
        notice_text = "\n".join(notices)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                parsed["_distill_notices"] = notices
                return json.dumps(parsed, indent=2)
            if parsed is not None:
                # JSON but not an object (array/number/…) — wrap so the
                # original value survives alongside the notices.
                return json.dumps({"_result": parsed, "_distill_notices": notices}, indent=2)
            return result + "\n\n" + notice_text
        if isinstance(result, dict):
            enriched = dict(result)
            enriched["_distill_notices"] = notices
            return enriched
        # Other return types (not str/dict): the drained notices cannot be
        # attached and are discarded. No @captured tool returns such a type
        # today; if one is added, give it a str or dict return so its notices
        # surface.
        return result
    except Exception:
        return result


def captured(source: str):
    """Decorator: log tool_call + tool_success/tool_error around an MCP tool;
    inline-trigger extraction when threshold met.

    Logging failures NEVER propagate.
    Inline-trigger failures NEVER propagate.
    """
    def decorator(fn: Callable) -> Callable:
        tool_name = fn.__name__
        full_fidelity = tool_name in FULL_FIDELITY_TOOLS

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            project_dir = _resolve_project_dir(kwargs.get("project_dir"))
            mgr = None
            try:
                mgr = get_session_manager(project_dir)
                mgr.record("tool_call", {
                    "source": source,
                    "tool": tool_name,
                    **_call_fields(full_fidelity, args, kwargs),
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
                            "capture_version": (
                                CAPTURE_VERSION_FULL
                                if full_fidelity
                                else CAPTURE_VERSION_LEGACY
                            ),
                            "error": f"{type(e).__name__}: {str(e)[:500]}",
                            "error_truncated": len(str(e)) > 500,
                        })
                except Exception:
                    pass
                _maybe_inline_extract(mgr, project_dir)
                raise

            duration_ms = int((time.time() - t0) * 1000)
            try:
                if mgr is not None:
                    mgr.record("tool_success", {
                        "source": source,
                        "tool": tool_name,
                        "duration_ms": duration_ms,
                        **_result_fields(full_fidelity, result),
                    })
            except Exception:
                pass
            _maybe_inline_extract(mgr, project_dir)
            return _attach_distill_notices(result, project_dir)

        return wrapper

    return decorator


def _maybe_inline_extract(mgr, project_dir: str) -> None:
    """Inline trigger: if should_extract returns True, fire commit().
    All exceptions swallowed — extraction failures must never block tools."""
    if mgr is None:
        return
    if os.environ.get("MAGNOLIA_DISABLE_INLINE_EXTRACT"):
        return
    try:
        from compchem_memory.extraction import AutomaticMemoryExtractor
        log_path = mgr.get_session_log_path()
        if not log_path:
            return
        extractor = AutomaticMemoryExtractor(project_dir)
        if extractor.should_extract(Path(log_path)):
            extractor.commit(Path(log_path), project_dir)
    except Exception:
        pass
