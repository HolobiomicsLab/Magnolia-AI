"""Shared capture infrastructure: per-project SessionManager registry + decorator (Task 2)."""

import json
import os
import time
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from compchem_memory.tiers.session import SessionManager
from compchem_memory import distill_log
from compchem_memory.storage import resolve_project_dir

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


# Tools whose result may be WITHHELD and replaced by the review envelope.
# Read-only and cheap to re-call: the agent relays the notice, then calls the
# tool again and gets the real result. Write tools (record_*, confirm, apply_*,
# consolidate, ...) are never withheld — hiding their result could cause a
# retry and a double write. The review tools themselves (review_consolidation,
# review_promotions) are the relay action, so they are excluded too.
_ENVELOPE_TOOLS = frozenset({
    "memory_get_context",
    "memory_search",
    "memory_search_errors",
    "memory_get_run_history",
    "memory_get_goal",
    "memory_select_relevant",
    "memory_scan_headers",
    "memory_health_check",
    "memory_notebook",
})

_RELAY_LATCH_NAME = ".relay-latch.json"


def _today() -> str:
    """Injectable clock for tests."""
    return date.today().isoformat()


def _pending_review_counts(project_dir: str) -> dict[str, int]:
    """Count proposals waiting for review in the project's reflex artifacts.
    Mirrors startup_scan._has_pending_review but reports per-kind counts.
    Never raises."""
    counts: dict[str, int] = {}
    try:
        from compchem_memory.reflex_common import pending_indices
        reflex = Path(project_dir) / ".magnolia" / "reflex"
        for kind, name in (("consolidation", "consolidation-proposal.json"),
                           ("promotion", "promotion-proposal.json")):
            artifact = reflex / name
            if not artifact.exists():
                continue
            try:
                data = json.loads(artifact.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            n = len(pending_indices(data))
            if n:
                counts[f"{kind}_proposals"] = n
    except Exception:
        return {}
    return counts


def _relay_latched_today(project_dir: str, today: str) -> bool:
    """True if the envelope already fired for this project today."""
    latch = Path(project_dir) / ".magnolia" / "reflex" / _RELAY_LATCH_NAME
    try:
        return json.loads(latch.read_text()).get("last_envelope") == today
    except (json.JSONDecodeError, OSError):
        return False


def _mark_relay_latch(project_dir: str, today: str) -> bool:
    """Record today as an envelope day. False on write failure (caller then
    falls back to the legacy notice tail instead of blocking every call)."""
    latch = Path(project_dir) / ".magnolia" / "reflex" / _RELAY_LATCH_NAME
    try:
        latch.parent.mkdir(parents=True, exist_ok=True)
        latch.write_text(json.dumps({"last_envelope": today}))
        return True
    except OSError:
        return False


def _review_envelope(project_dir: str, tool_name: str,
                     counts: dict[str, int], notices: list[str]) -> str:
    total = sum(counts.values())
    kinds = " and ".join(f"{n} {k.replace('_proposals', '')} proposal(s)"
                         for k, n in counts.items())
    return json.dumps({
        "status": "review_required",
        "pending": counts,
        "withheld_tool": tool_name,
        "notices": notices,
        "message": (
            f"{total} memory review proposal(s) are waiting ({kinds}). "
            "Memory reviews are messages for the user: tell them now, in plain "
            "words, what is waiting and ask whether to review it. Nothing is "
            "applied without the user's confirmation. The real tool result was "
            "withheld — after relaying, call this tool again to get it."
        ),
    }, indent=2)


def _attach_distill_notices(result: Any, project_dir: str,
                            tool_name: str = "") -> Any:
    """Surface pending review proposals and drained notices on a tool result.

    Two channels, in priority order:

    1. BLOCKING ENVELOPE — pending proposals + read-only tool + not yet fired
       today: replace the result with a ``status: review_required`` payload
       (the anatomy of project_switch_blocked, which agents reliably relay;
       an advisory tail on a large success payload was ignored 8 times over
       260 replies — see staging 20260930_153812). Fires once per project per
       day (latch: .magnolia/reflex/.relay-latch.json).
    2. LEGACY TAIL — otherwise attach drained notices as ``_distill_notices``.
       While the latch is set (envelope already fired today), the repeated
       tail nudges are suppressed: identical repeats across a day read as
       already-handled (habituation was a measured failure factor).

    Never raises.
    """  # noqa: E501
    try:
        notices = distill_log.drain_distill_notices(project_dir)
        counts = _pending_review_counts(project_dir)
        if counts:
            today = _today()
            latched = _relay_latched_today(project_dir, today)
            if tool_name in _ENVELOPE_TOOLS and not latched:
                if _mark_relay_latch(project_dir, today):
                    return _review_envelope(project_dir, tool_name, counts, notices)
            if latched:
                # Envelope already fired today: suppress the duplicate tail
                # nudges (repeats read as already-handled), keep the result.
                return result
            # Latch write failed, or a non-withholdable tool hit a fresh day:
            # fall through to the legacy tail so notices still surface.
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


def _reported_failure(result: Any) -> str | None:
    """Return the error text when a tool RETURNED a failure payload.

    The compchem-tools wrappers signal failure with ``{"success": false,
    "error": ...}`` instead of raising. Journaling that as ``tool_success``
    hides it from the error-resolution logic (extraction.py, audit.py), which
    keys on ``tool_error`` events. So inspect the payload here: a JSON string
    is parsed, a dict is read directly; anything else is a success.
    """
    payload = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except (ValueError, TypeError):
            return None
    if isinstance(payload, dict) and payload.get("success") is False:
        error = payload.get("error") or payload.get("message") or "success=false"
        return str(error)[:500]
    return None


def captured(source: str):
    """Decorator: log tool_call + tool_success/tool_error around an MCP tool;
    inline-trigger extraction when threshold met.

    Logging failures NEVER propagate.
    Inline-trigger failures NEVER propagate.
    """
    def decorator(fn: Callable) -> Callable:
        tool_name = fn.__name__

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            project_dir = _resolve_project_dir(kwargs.get("project_dir"))
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
                _maybe_inline_extract(mgr, project_dir)
                raise

            duration_ms = int((time.time() - t0) * 1000)
            failure = _reported_failure(result)
            try:
                if mgr is not None:
                    if failure is not None:
                        mgr.record("tool_error", {
                            "source": source,
                            "tool": tool_name,
                            "duration_ms": duration_ms,
                            "error": f"ToolReportedFailure: {failure}",
                            "result_summary": _summarize_result(result),
                        })
                    else:
                        mgr.record("tool_success", {
                            "source": source,
                            "tool": tool_name,
                            "duration_ms": duration_ms,
                            "result_summary": _summarize_result(result),
                        })
            except Exception:
                pass
            _maybe_inline_extract(mgr, project_dir)
            return _attach_distill_notices(result, project_dir, tool_name)

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
