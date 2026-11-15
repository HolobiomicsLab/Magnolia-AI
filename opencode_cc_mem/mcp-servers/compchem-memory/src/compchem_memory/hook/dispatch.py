"""Route neutral hook events through Magnolia's portable behaviours.

Turn-end deliberately runs the critic last and may therefore be slow. Client
adapters may run that event asynchronously; Claude Code supports ``async: true``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from compchem_memory.hook.events import HookEvent, HookResponse
from compchem_memory.hook.project import bind_project

if TYPE_CHECKING:
    from compchem_memory.hook.action_retrieval import ActionNote
    from compchem_memory.hook.project import ProjectPaths


@dataclass
class Handled:
    """A client-independent response and any not-yet-exposed action note."""

    response: HookResponse
    pending: ActionNote | None


def _record(paths: ProjectPaths, event: HookEvent) -> None:
    """Record the session idempotently, then append the current event."""
    from compchem_memory.hook.sessions import record_session
    from compchem_memory.hook.transcript import append_event

    record_session(
        paths.store, event.harness, event.session_id, event.cwd, event.transcript_ref,
    )
    append_event(paths.store, event)


def _directive(paths: ProjectPaths, event: HookEvent) -> str | None:
    """Return the session's one-time retrieval directive."""
    from compchem_memory.hook.directive import retrieval_directive
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        return retrieval_directive(paths, state, event.session_id)


def _notices(paths: ProjectPaths, event: HookEvent) -> str | None:
    """Consume pending notices in one state transaction."""
    from compchem_memory.hook.notices import pending_notices
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        return pending_notices(paths, state)


def _session_start(paths: ProjectPaths, event: HookEvent) -> Handled:
    from compchem_memory.hook.boot_context import build_boot_payload

    response = HookResponse(context=build_boot_payload(paths))
    response = response.merge(HookResponse(context=_directive(paths, event)))
    return Handled(response, None)


def _prompt(paths: ProjectPaths, event: HookEvent, env: Mapping[str, str]) -> Handled:
    from compchem_memory.hook.doorbell import process_prompt

    result = process_prompt(event.prompt or "", paths, env)
    context = None
    if result.sent:
        mentions = ", ".join(result.sent)
        context = (
            "The following @mentions were already sent to helper agents; "
            f"do not act on them: {mentions}"
        )
    response = HookResponse(context=context)
    response = response.merge(HookResponse(context=_directive(paths, event)))
    response = response.merge(HookResponse(notice=_notices(paths, event)))
    return Handled(response, None)


def _post_tool(paths: ProjectPaths, event: HookEvent, deadline: float) -> Handled:
    from compchem_memory.hook.action_retrieval import on_post_tool

    pending = on_post_tool(event, paths, deadline)
    response = HookResponse(tool_note=None if pending is None else pending.text)
    response = response.merge(HookResponse(notice=_notices(paths, event)))
    return Handled(response, pending)


def _turn_end(paths: ProjectPaths, event: HookEvent) -> Handled:
    from compchem_memory.hook.action_retrieval import probe_application
    from compchem_memory.hook.critic import run_critic

    probe_application(
        paths, event.harness, event.session_id, event.assistant_text or "",
    )
    agent_id = None if event.agent is None else event.agent.id
    run_critic(paths, event.harness, event.session_id, agent_id)
    return Handled(HookResponse(), None)


def handle(event: HookEvent, env: Mapping[str, str], deadline: float) -> Handled:
    """Bind and record an event, then return its neutral hook response."""
    paths = bind_project(event.harness, event.session_id, Path(event.cwd), env)
    if paths is None:
        return Handled(HookResponse(), None)
    _record(paths, event)
    if event.event == "session-start":
        return _session_start(paths, event)
    if event.event == "prompt":
        return _prompt(paths, event, env)
    if event.event == "post-tool":
        return _post_tool(paths, event, deadline)
    if event.event == "turn-end":
        return _turn_end(paths, event)
    return Handled(HookResponse(), None)

