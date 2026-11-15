"""Route neutral hook events through Magnolia's portable behaviours.

Turn-end deliberately runs the critic last and may therefore be slow. Client
adapters may run that event asynchronously; Claude Code supports ``async: true``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from compchem_memory.hook.events import HookEvent, HookResponse
from compchem_memory.hook.project import bind_project

if TYPE_CHECKING:
    from compchem_memory.hook.action_retrieval import ActionNote
    from compchem_memory.hook.notices import Notices
    from compchem_memory.hook.project import ProjectPaths


@dataclass
class Handled:
    """A client-independent response and what it consumes once delivered.

    ``pending`` (an action note) and ``notices`` are committed by the CLI only
    when the adapter reports that it emitted ``tool_note`` or ``notice``.
    """

    response: HookResponse
    pending: ActionNote | None
    notices: Notices | None = field(default=None)
    paths: ProjectPaths | None = field(default=None)


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


def _notices(paths: ProjectPaths, event: HookEvent) -> Notices | None:
    """Read pending notices; the CLI consumes them after delivery."""
    from compchem_memory.hook.notices import peek_notices
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        return peek_notices(paths, state)


def _session_start(paths: ProjectPaths, event: HookEvent) -> Handled:
    from compchem_memory.hook.boot_context import build_boot_payload

    response = HookResponse(context=build_boot_payload(paths))
    response = response.merge(HookResponse(context=_directive(paths, event)))
    return Handled(response, None)


# Prompts a client submits on the agent's behalf, not typed by the person:
# Claude Code's sub-agent hand-back (observed, ``claude/parent/21-UserPromptSubmit.json``)
# and its background-task notification turn (its result line has
# ``origin.kind: task-notification``; the prompt itself was not captured, so that
# prefix is defensive). Provisional until the Claude adapter (Plan 2) marks a
# prompt's origin itself.
_HARNESS_PROMPT_PREFIXES = ("<agent-message ", "<agent-message>", "<task-notification>")


def is_person_prompt(event: HookEvent) -> bool:
    """Whether a prompt event is the main agent's prompt from the person.

    A sub-agent's prompt (``event.agent`` set, as Codex sends inside a spawned
    agent) and a harness-generated prompt are not: model output must never ring
    the doorbell, take the session's one directive or consume notices.
    """
    if event.agent is not None:
        return False
    return not (event.prompt or "").lstrip().startswith(_HARNESS_PROMPT_PREFIXES)


def _prompt(paths: ProjectPaths, event: HookEvent, env: Mapping[str, str]) -> Handled:
    from compchem_memory.hook.doorbell import process_prompt

    if not is_person_prompt(event):
        return Handled(HookResponse(), None)
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
    notices = _notices(paths, event)
    response = response.merge(HookResponse(notice=None if notices is None else notices.text))
    return Handled(response, None, notices)


def _post_tool(paths: ProjectPaths, event: HookEvent, deadline: float) -> Handled:
    from compchem_memory.hook.action_retrieval import on_post_tool

    pending = on_post_tool(event, paths, deadline)
    response = HookResponse(tool_note=None if pending is None else pending.text)
    notices = _notices(paths, event) if event.agent is None else None
    response = response.merge(HookResponse(notice=None if notices is None else notices.text))
    return Handled(response, pending, notices)


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
        handled = _session_start(paths, event)
    elif event.event == "prompt":
        handled = _prompt(paths, event, env)
    elif event.event == "post-tool":
        handled = _post_tool(paths, event, deadline)
    elif event.event == "turn-end":
        handled = _turn_end(paths, event)
    else:
        handled = Handled(HookResponse(), None)
    handled.paths = paths
    return handled

