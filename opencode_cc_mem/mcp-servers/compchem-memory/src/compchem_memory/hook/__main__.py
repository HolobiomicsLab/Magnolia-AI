"""Fail-open command-line lifecycle for Magnolia hook adapters."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

POST_TOOL_SECONDS = 2.5
POST_TOOL_EXIT_RESERVE_SECONDS = 0.2
DEFAULT_SECONDS = 8.0


def _render_handled(adapter: Any, handled: Any, event: Any):
    """Render a handled response without recording delivery yet."""
    return adapter.render(handled.response, event)


def _commit_delivered(rendered: Any, handled: Any, event: Any) -> None:
    """Consume what the adapter reports as emitted; keep or discard the rest.

    An action note that was not emitted spends no budget and is logged with
    ``injected: 0``; notices that were not emitted stay queued for the next event.
    """
    if handled.pending is not None:
        from compchem_memory.hook.action_retrieval import commit_exposure, discard_note

        if "tool_note" in rendered.emitted:
            commit_exposure(handled.pending, event.session_id)
        else:
            discard_note(handled.pending)
    notices = getattr(handled, "notices", None)
    paths = getattr(handled, "paths", None)
    if notices is not None and paths is not None and "notice" in rendered.emitted:
        from compchem_memory.hook.notices import commit_notices

        commit_notices(paths, event.harness, event.session_id, notices)


def _write_rendered(rendered: Any, handled: Any, event: Any) -> None:
    """Write UTF-8 output, then commit what was delivered."""
    sys.stdout.buffer.write(rendered.stdout.encode("utf-8"))
    sys.stdout.buffer.flush()
    _commit_delivered(rendered, handled, event)
    sys.stderr.buffer.write(rendered.stderr.encode("utf-8"))
    sys.stderr.buffer.flush()


def _bound_store(event: Any, env: Mapping[str, str]) -> Path | None:
    """Return the event's bound store, or ``None`` when unbound or unbindable."""
    from compchem_memory.hook.project import bind_project

    if event is None:
        return None
    try:
        paths = bind_project(event.harness, event.session_id, Path(event.cwd), env)
    except Exception:  # noqa: BLE001 - error logging must retain its fallback
        return None
    return None if paths is None else paths.store


def _log_error(
    error: Exception,
    harness: str,
    event_name: str,
    event: Any,
    env: Mapping[str, str],
) -> None:
    """Append one payload-free error row to the store, else the state home; never raise."""
    try:
        from compchem_memory.hook.errors import log_error

        log_error(error, harness, event_name, _bound_store(event, env), env)
    except Exception:  # noqa: BLE001 - logging is the last resort
        pass


def _parse_stdin(harness: str, event_name: str):
    """Return the selected adapter and its parsed stdin event."""
    from compchem_memory.hook.adapters import ADAPTERS

    adapter = ADAPTERS[harness]
    raw = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise ValueError("hook payload must be an object")
    event = adapter.parse({**raw, "harness": harness}, event_name)
    return adapter, event


def main() -> None:
    """Run one hook event, always terminating without unwinding worker threads."""
    started = time.monotonic()
    harness = sys.argv[1] if len(sys.argv) > 1 else ""
    event_name = sys.argv[2] if len(sys.argv) > 2 else ""
    timeout = POST_TOOL_SECONDS if event_name == "post-tool" else DEFAULT_SECONDS
    deadline = started + timeout
    if event_name == "post-tool":
        deadline -= POST_TOOL_EXIT_RESERVE_SECONDS
    event = None
    try:
        adapter, event = _parse_stdin(harness, event_name)
        from compchem_memory.hook.dispatch import handle

        handled = handle(event, os.environ, deadline)
        rendered = _render_handled(adapter, handled, event)
        _write_rendered(rendered, handled, event)
    except Exception as error:  # noqa: BLE001 - every hook failure is fail-open
        _log_error(error, harness, event_name, event, os.environ)
        os._exit(0)
    os._exit(rendered.exit_code)


if __name__ == "__main__":
    main()
