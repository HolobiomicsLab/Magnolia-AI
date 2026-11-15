"""Fail-open command-line lifecycle for Magnolia hook adapters."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

POST_TOOL_SECONDS = 2.5
POST_TOOL_EXIT_RESERVE_SECONDS = 0.2
DEFAULT_SECONDS = 8.0


def _render_handled(adapter: Any, handled: Any, event: Any):
    """Render a handled response without recording delivery yet."""
    return adapter.render(handled.response, event)


def _write_rendered(rendered: Any, handled: Any, event: Any) -> None:
    """Write UTF-8 output and commit exposure after stdout delivery."""
    sys.stdout.buffer.write(rendered.stdout.encode("utf-8"))
    sys.stdout.buffer.flush()
    if handled.pending is not None and "tool_note" in rendered.emitted:
        from compchem_memory.hook.action_retrieval import commit_exposure

        commit_exposure(handled.pending, event.session_id)
    sys.stderr.buffer.write(rendered.stderr.encode("utf-8"))
    sys.stderr.buffer.flush()


def _error_destination(event: Any, env: Mapping[str, str]) -> Path:
    """Return the bound store, or Magnolia's state home when unbound."""
    from compchem_memory.hook.project import bind_project, state_home

    if event is not None:
        try:
            paths = bind_project(
                event.harness, event.session_id, Path(event.cwd), env,
            )
        except Exception:  # noqa: BLE001 - error logging must retain its fallback
            return state_home(env)
        if paths is not None:
            return paths.store
    return state_home(env)


def _log_error(
    error: Exception,
    harness: str,
    event_name: str,
    event: Any,
    env: Mapping[str, str],
) -> None:
    """Append one payload-free error row without raising."""
    try:
        destination = _error_destination(event, env)
        destination.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "harness": harness,
            "event": event_name,
            "error": f"{type(error).__name__}: {error}",
        }
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        descriptor = os.open(
            destination / "hook-errors.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
    except Exception:
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
