"""Inject the first-prompt memory retrieval directive once per session."""

import json
import os
from datetime import datetime, timezone
from typing import Mapping, Protocol

from compchem_memory.hook.project import ProjectPaths

DIRECTIVE = (
    "\n\n[Magnolia · auto-memory] Before acting on this request, your FIRST tool "
    "call must be `memory_get_context(task_description=\"<one-line summary of this "
    "request>\")`. Exception: for a pure recap request (status / pending tasks / "
    "where were we), answer from the SESSION HANDOVER already in your context and "
    "skip the call. Otherwise the returned project entries and skill-tier rules "
    "inform your approach — accumulated learnings often hold the fix, the right "
    "parameters, or a known pitfall for this exact task, and the longer the project "
    "runs the more likely that is. (This call is read-only and safe even if the "
    "turn is exploratory; it also surfaces any pending memory-review proposals.)"
)
_DISABLED_VALUES = {"0", "off", "false", "no"}


class _DirectiveState(Protocol):
    data: dict


def _disabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get("MAGNOLIA_AUTORETRIEVE", "").lower() in _DISABLED_VALUES


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_ledger(paths: ProjectPaths, session_id: str) -> None:
    paths.store.mkdir(parents=True, exist_ok=True)
    row = {"ts": _timestamp(), "sessionID": session_id, "action": "inject"}
    data = (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(paths.store / "auto-retrieval.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def retrieval_directive(paths: ProjectPaths, state: _DirectiveState, session_id: str) -> str | None:
    """Return the directive once, recording injection in state and the ledger."""
    if _disabled() or state.data.get("directive_sent"):
        return None
    try:
        _append_ledger(paths, session_id)
    except OSError:
        return None
    state.data["directive_sent"] = True
    return DIRECTIVE

