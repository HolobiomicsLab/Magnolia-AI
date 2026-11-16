"""Magnolia hook CLI: interactive fail-open and required role fail-closed."""

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
    pending = getattr(handled, "pending", None)
    is_vibe_mcp = (
        event.harness == "vibe" and event.event == "post-tool"
        and event.tool is not None and event.tool.raw_name.startswith("mcp_")
        and "." in event.tool.raw_name.removeprefix("mcp_")
    )
    if pending is not None:
        from compchem_memory.hook.action_retrieval import commit_exposure, discard_note

        if "tool_note" in rendered.emitted:
            commit_exposure(pending, event.session_id)
        elif is_vibe_mcp and handled.paths is not None:
            from compchem_memory.hook.dispatch import stash_carried_note

            stash_carried_note(handled.paths, event, pending)
        else:
            discard_note(pending)
    carried = getattr(handled, "carried", [])
    carried_field = getattr(handled, "carried_field", None)
    if carried and handled.paths is not None:
        from compchem_memory.hook.action_retrieval import commit_exposure
        from compchem_memory.hook.dispatch import clear_carried_notes, release_carried_notes

        if carried_field in rendered.emitted:
            for item in carried:
                commit_exposure(item.note, event.session_id)
            clear_carried_notes(handled.paths, event, [item.id for item in carried])
        else:
            release_carried_notes(handled.paths, event, [item.id for item in carried])
    notices = getattr(handled, "notices", None)
    paths = getattr(handled, "paths", None)
    if notices is not None:
        if paths is not None and "notice" in rendered.emitted:
            from compchem_memory.hook.notices import commit_notices

            commit_notices(paths, event.harness, event.session_id, notices)
        else:
            from compchem_memory.hook.notices import release_notices

            release_notices(notices)
    if paths is not None:
        from compchem_memory.hook.transcript import append_injected

        for injection in getattr(handled, "injections", []):
            if injection.field not in rendered.emitted:
                continue
            append_injected(
                paths.store, event, channel=injection.channel, text=injection.text,
                entry_ids=injection.entry_ids, tool_call_id=injection.tool_call_id,
            )


def _release_unsettled(
    handled: Any,
    event: Any = None,
    *,
    release_carried: bool = True,
    release_notice: bool = True,
) -> None:
    """Release action-note and notice reservations after a failure; never raise."""
    pending = getattr(handled, "pending", None)
    if pending is not None:
        try:
            from compchem_memory.hook.action_retrieval import discard_note

            discard_note(pending)
        except Exception:  # noqa: BLE001 - the error row is already written
            pass
    notices = getattr(handled, "notices", None)
    paths = getattr(handled, "paths", None)
    carried = getattr(handled, "carried", [])
    if release_carried and carried and paths is not None and event is not None:
        try:
            from compchem_memory.hook.dispatch import release_carried_notes

            release_carried_notes(paths, event, [item.id for item in carried])
        except Exception:  # noqa: BLE001 - the error row is already written
            pass
    if release_notice and notices is not None:
        try:
            from compchem_memory.hook.notices import release_notices

            release_notices(notices)
        except Exception:  # noqa: BLE001 - process exit also permits stale recovery
            pass


def _settle_emitted_after_commit_failure(rendered: Any, handled: Any, event: Any) -> None:
    """Best-effort settle context that already reached the client."""
    paths = getattr(handled, "paths", None)
    carried = getattr(handled, "carried", [])
    carried_field = getattr(handled, "carried_field", None)
    carried_was_emitted = carried_field in rendered.emitted
    _release_unsettled(
        handled, event,
        release_carried=not carried_was_emitted,
        release_notice=False,
    )
    if carried and paths is not None and carried_was_emitted:
        try:
            from compchem_memory.hook.dispatch import clear_carried_notes

            clear_carried_notes(paths, event, [item.id for item in carried])
        except Exception:  # noqa: BLE001 - stdout delivery already happened
            pass
    notices = getattr(handled, "notices", None)
    if notices is not None:
        try:
            from compchem_memory.hook.notices import commit_notices, release_notices

            if paths is not None and "notice" in rendered.emitted:
                commit_notices(paths, event.harness, event.session_id, notices)
            else:
                release_notices(notices)
        except Exception:  # noqa: BLE001 - stdout delivery already happened
            pass


def _write_rendered(
    rendered: Any,
    handled: Any,
    event: Any,
    env: Mapping[str, str] | None = None,
) -> None:
    """Write UTF-8 output and isolate delivery-commit failures."""
    sys.stdout.buffer.write(rendered.stdout.encode("utf-8"))
    sys.stdout.buffer.flush()
    if handled is not None:
        try:
            _commit_delivered(rendered, handled, event)
        except Exception as error:  # noqa: BLE001 - rendered denial must keep its exit code
            _log_error(error, event.harness, event.event, event, env or os.environ)
            # stdout already reached the client. Re-queueing this context can
            # deliver it twice; keep the at-most-once direction on commit failure.
            _settle_emitted_after_commit_failure(rendered, handled, event)
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


def _log_after_emission(
    error: Exception,
    harness: str,
    event_name: str,
    event: Any,
    env: Mapping[str, str],
) -> None:
    """Log after required denial delivery without changing its result."""
    try:
        _log_error(error, harness, event_name, event, env)
    except Exception:  # noqa: BLE001 - diagnostics cannot override a delivered denial
        pass


def _required_failure_event(harness: str, event_name: str, raw: Any = None):
    """Build the minimum event needed for a native required denial."""
    from compchem_memory.hook.events import HookEvent, ToolCall

    payload = raw if isinstance(raw, dict) else {}
    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", ".")
    if not isinstance(session_id, str) or not session_id:
        session_id = "unknown"
    if not isinstance(cwd, str) or not cwd:
        cwd = "."
    neutral_event = event_name if event_name in HookEvent.EVENTS else "pre-tool"
    return HookEvent(
        harness,
        neutral_event,
        session_id,
        cwd,
        tool=ToolCall("unknown", None, "unknown", None, {}),
    )


def _render_required_failure(
    adapter: Any,
    harness: str,
    event_name: str,
    env: Mapping[str, str],
    raw: Any = None,
    diagnostic: Exception | None = None,
) -> int:
    """Emit a native denial, or a nonzero hook failure if emission is impossible."""
    if event_name != "pre-tool":
        return 0
    from compchem_memory.hook.events import Block, HookResponse
    from compchem_memory.hook.policy import REQUIRED_REASON

    event = _required_failure_event(harness, event_name, raw)
    response = HookResponse(block=Block(REQUIRED_REASON))
    try:
        rendered = adapter.render(response, event)
    except Exception:  # noqa: BLE001 - required mode must not pass
        return 2
    if not rendered.stdout:
        return 2
    try:
        _write_rendered(rendered, None, event, env)
    except Exception:  # noqa: BLE001 - failed emission cannot be an exit-0 pass
        return 2
    if diagnostic is not None:
        _log_after_emission(diagnostic, harness, event_name, event, env)
    _record_denied(event, REQUIRED_REASON, env)
    return rendered.exit_code


def _native_tool_name(raw: dict) -> str | None:
    """Extract a native tool name without assuming a client payload schema."""
    candidates = [raw.get("tool_name")]
    native_tool = raw.get("tool")
    if isinstance(native_tool, dict):
        candidates.extend((native_tool.get("raw_name"), native_tool.get("name")))
    return next((name for name in candidates if isinstance(name, str) and name), None)


def _recovered_pre_tool(raw: dict, harness: str, event_name: str, env: Mapping[str, str]):
    """Build the minimum policy event for an otherwise unparseable pre-tool call."""
    if event_name != "pre-tool" or not env.get("MAGNOLIA_HOOK_POLICY"):
        return None
    raw_name = _native_tool_name(raw)
    if raw_name is None:
        return None
    from compchem_memory.hook.events import HookEvent, ToolCall, canonical_tool

    server, name = canonical_tool(raw_name)
    cwd = raw.get("cwd") if isinstance(raw.get("cwd"), str) and raw.get("cwd") else "."
    tool = ToolCall(name, server, raw_name, None, {})
    return HookEvent(harness, "pre-tool", "unknown", cwd, tool=tool)


def _render_recovered_policy(adapter: Any, event: Any, env: Mapping[str, str]) -> int:
    """Render a recovered policy block, returning zero when no denial can be emitted."""
    if event is None:
        return 0
    response, diagnostic = _policy_response(event, env)
    if response.block is None:
        return 0
    return _render_policy_block(adapter, event, response, env, diagnostic=diagnostic)


def _render_policy_block(
    adapter: Any,
    event: Any,
    response: Any,
    env: Mapping[str, str],
    *,
    diagnostic: Exception | None = None,
) -> int:
    """Write a known denial immediately, before optional hook side effects."""
    is_required = env.get("MAGNOLIA_HOOK_POLICY_REQUIRED") == "1"
    try:
        rendered = adapter.render(response, event)
    except Exception as error:  # noqa: BLE001 - required mode converts this to hook failure
        if not is_required:
            _log_error(error, event.harness, event.event, event, env)
        return 2 if is_required else 0
    if is_required and not rendered.stdout:
        return 2
    try:
        _write_rendered(rendered, None, event, env)
    except Exception as error:  # noqa: BLE001 - preserve the rendered denial exit code
        if not is_required:
            _log_error(error, event.harness, event.event, event, env)
        return 2 if is_required else rendered.exit_code
    if is_required and diagnostic is not None:
        _log_after_emission(diagnostic, event.harness, event.event, event, env)
    _record_denied(event, response.block.reason, env)
    return rendered.exit_code


def _record_denied(event: Any, reason: str, env: Mapping[str, str]) -> None:
    """Record policy evidence only after the client received the denial."""
    if event.session_id == "unknown":
        return
    store = _bound_store(event, env)
    if store is None:
        return
    try:
        from compchem_memory.hook.transcript import append_denied

        append_denied(store, event, reason)
    except Exception as error:  # noqa: BLE001 - denial already reached the client
        _log_error(error, event.harness, event.event, event, env)


def _policy_response(event: Any, env: Mapping[str, str]) -> tuple[Any, Exception | None]:
    """Evaluate policy independently, failing closed for configured shell calls."""
    from compchem_memory.hook.events import Block, HookResponse, canonical_tool
    from compchem_memory.hook.policy import (
        REASON,
        REQUIRED_REASON,
        decide,
        required_mode,
    )

    try:
        return HookResponse(block=decide(event, env)), None
    except Exception as error:  # noqa: BLE001 - configured bash policy fails closed
        if required_mode(env):
            return HookResponse(block=Block(REQUIRED_REASON)), error
        _log_error(error, event.harness, event.event, event, env)
        is_bash = (
            event.event == "pre-tool"
            and event.tool is not None
            and canonical_tool(event.tool.raw_name) == (None, "bash")
        )
        block = Block(REASON) if env.get("MAGNOLIA_HOOK_POLICY") and is_bash else None
        return HookResponse(block=block), None


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
    handled = None
    is_required = os.environ.get("MAGNOLIA_HOOK_POLICY_REQUIRED") == "1"
    try:
        from compchem_memory.hook.adapters import ADAPTERS

        adapter = ADAPTERS[harness]
    except Exception as error:  # noqa: BLE001 - unknown required harness must refuse
        if is_required and event_name == "pre-tool":
            os._exit(2)
        _log_error(error, harness, event_name, event, os.environ)
        os._exit(0)
    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raise ValueError("hook payload must be an object")
    except Exception as error:  # noqa: BLE001 - required input failures deny natively
        if is_required:
            code = _render_required_failure(
                adapter,
                harness,
                event_name,
                os.environ,
                locals().get("raw"),
                diagnostic=error,
            )
            os._exit(code)
        _log_error(error, harness, event_name, event, os.environ)
        os._exit(0)
    try:
        event = adapter.parse({**raw, "harness": harness}, event_name)
    except Exception as error:  # noqa: BLE001 - recover only a configured pre-tool policy
        if is_required:
            code = _render_required_failure(
                adapter,
                harness,
                event_name,
                os.environ,
                raw,
                diagnostic=error,
            )
            os._exit(code)
        _log_error(error, harness, event_name, event, os.environ)
        recovered = _recovered_pre_tool(raw, harness, event_name, os.environ)
        os._exit(_render_recovered_policy(adapter, recovered, os.environ))
    if event is None:
        if is_required:
            if raw.get("tool_name") in {"ToolSearch", "SubagentHandback"}:
                os._exit(0)
            os._exit(
                _render_required_failure(
                    adapter,
                    harness,
                    event_name,
                    os.environ,
                    raw,
                    diagnostic=ValueError("adapter returned no event"),
                )
            )
        recovered = _recovered_pre_tool(raw, harness, event_name, os.environ)
        # An ignored event is an error only when the policy blocks its tool
        # (e.g. not for Claude's ToolSearch events).
        recovered_response, _ = (
            _policy_response(recovered, os.environ)
            if recovered is not None
            else (None, None)
        )
        if recovered_response is not None and recovered_response.block is not None:
            _log_error(
                ValueError("adapter returned no event for configured pre-tool policy"),
                harness,
                event_name,
                recovered,
                os.environ,
            )
        os._exit(_render_recovered_policy(adapter, recovered, os.environ))

    policy_response, policy_diagnostic = _policy_response(event, os.environ)
    if policy_response.block is not None:
        os._exit(
            _render_policy_block(
                adapter,
                event,
                policy_response,
                os.environ,
                diagnostic=policy_diagnostic,
            )
        )
    try:
        from compchem_memory.hook.dispatch import handle

        handled = handle(event, os.environ, deadline)
    except Exception as error:  # noqa: BLE001 - policy denial survives dispatch failure
        _log_error(error, harness, event_name, event, os.environ)
        if policy_response.block is None:
            _release_unsettled(handled, event)
            os._exit(0)
        try:
            rendered = adapter.render(policy_response, event)
        except Exception as render_error:  # noqa: BLE001 - no client denial can be emitted
            _log_error(render_error, harness, event_name, event, os.environ)
            _release_unsettled(handled, event)
            os._exit(0)
        try:
            _write_rendered(rendered, handled, event, os.environ)
        except Exception as write_error:  # noqa: BLE001 - a rendered shell denial stays closed
            _log_error(write_error, harness, event_name, event, os.environ)
            _release_unsettled(handled, event)
        os._exit(rendered.exit_code)

    handled.response = policy_response.merge(handled.response)
    try:
        rendered = _render_handled(adapter, handled, event)
    except Exception as render_error:  # noqa: BLE001 - adapter could not produce a denial
        _log_error(render_error, harness, event_name, event, os.environ)
        _release_unsettled(handled, event)
        os._exit(0)
    try:
        _write_rendered(rendered, handled, event, os.environ)
    except Exception as write_error:  # noqa: BLE001 - only shell policy remains fail-closed
        _log_error(write_error, harness, event_name, event, os.environ)
        _release_unsettled(handled, event)
        if policy_response.block is None:
            os._exit(0)
    os._exit(rendered.exit_code)


if __name__ == "__main__":
    main()
