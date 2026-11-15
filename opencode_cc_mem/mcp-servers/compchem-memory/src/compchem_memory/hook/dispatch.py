"""Route neutral hook events through Magnolia's portable behaviours.

Turn-end deliberately runs the critic last and may therefore be slow. Client
adapters may run that event asynchronously; Claude Code supports ``async: true``.

Behaviours are isolated from each other, as the OpenCode plugins are: each runs
under its own guard, a failure leaves one row in ``hook-errors.jsonl`` (in the
store, or in Magnolia's state home when the store is unwritable) and the other
behaviours still run, so a failing transcript or directive ledger never costs
the boot context.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from compchem_memory.hook.events import HookEvent, HookResponse
from compchem_memory.hook.project import bind_project

if TYPE_CHECKING:
    from compchem_memory.hook.action_retrieval import ActionNote
    from compchem_memory.hook.notices import Notices
    from compchem_memory.hook.project import ProjectPaths

_RECOVERED_TURN_LIMIT = 200


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


class _Guard:
    """Run one behaviour; log its failure and return a default instead of raising."""

    def __init__(self, paths: ProjectPaths, event: HookEvent, env: Mapping[str, str]) -> None:
        self.paths = paths
        self.event = event
        self.env = env

    def __call__(self, behaviour: str, function: Callable[..., Any], *args: Any, default: Any = None) -> Any:
        try:
            return function(*args)
        except Exception as error:  # noqa: BLE001 - one behaviour must not stop the others
            from compchem_memory.hook.errors import log_error

            log_error(error, self.event.harness, self.event.event, self.paths.store, self.env, behaviour)
            return default


def _record_session(paths: ProjectPaths, event: HookEvent) -> None:
    from compchem_memory.hook.sessions import record_session

    record_session(
        paths.store, event.harness, event.session_id, event.cwd, event.transcript_ref,
    )


def _append_transcript(paths: ProjectPaths, event: HookEvent) -> None:
    from compchem_memory.hook.transcript import append_event

    append_event(paths.store, event)


def _normalise_reply(text: str) -> str:
    """Normalise stored text for comparison without changing recorded content."""
    from compchem_memory.hook.transcript import clean_text

    return " ".join(clean_text(text).split())


def _reply_candidates(event: HookEvent, recovered: list[str]) -> list[str]:
    """Return recovered blocks followed by the native final reply, if any."""
    if event.harness == "vibe" and event.assistant_text is None:
        event.assistant_text = recovered[-1]

    candidates = list(recovered)
    if not event.assistant_text:
        return candidates
    final_text = _normalise_reply(event.assistant_text)
    for index in range(len(candidates) - 1, -1, -1):
        if _normalise_reply(candidates[index]) == final_text:
            candidates.pop(index)
            break
    candidates.append(event.assistant_text)
    return candidates


def _existing_reply_counts(paths: ProjectPaths, event: HookEvent, agent_id: str | None) -> Counter:
    """Count comparable assistant rows already stored for this turn and agent."""
    from compchem_memory.hook.transcript import read_turns, transcript_path

    existing = Counter()
    path = transcript_path(paths.store, event.harness, event.session_id)
    for row in read_turns(path):
        row_agent = row.get("agent")
        row_agent_id = row_agent.get("id") if isinstance(row_agent, dict) else None
        text = row.get("text")
        if (row.get("kind") == "assistant" and row.get("turn_id") == event.turn_id
                and row_agent_id == agent_id and isinstance(text, str)):
            existing[_normalise_reply(text)] += 1
    return existing


def _append_missing_replies(
    paths: ProjectPaths, event: HookEvent, candidates: list[str], existing: Counter,
) -> None:
    """Append candidate occurrences not represented in existing assistant rows."""
    from compchem_memory.hook.transcript import append_event

    for text in candidates:
        normalised = _normalise_reply(text)
        if existing[normalised]:
            existing[normalised] -= 1
            continue
        append_event(paths.store, HookEvent(
            harness=event.harness,
            event="turn-end",
            session_id=event.session_id,
            cwd=event.cwd,
            schema_version=event.schema_version,
            turn_id=event.turn_id,
            agent=event.agent,
            assistant_text=text,
        ))


def _commit_recovered_replies(
    paths: ProjectPaths, event: HookEvent, agent_id: str | None, candidates: list[str],
) -> None:
    """Serialise reply deduplication and its bounded completion ledger."""
    from compchem_memory.hook.state import SessionState

    recovery_key = [event.session_id, event.turn_id, agent_id]
    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        recovered_turns = state.data.get("recovered_turns")
        if not isinstance(recovered_turns, list):
            recovered_turns = []
        was_recovered = recovery_key in recovered_turns
        existing = _existing_reply_counts(paths, event, agent_id)
        _append_missing_replies(paths, event, candidates, existing)
        if was_recovered:
            recovered_turns.remove(recovery_key)
        recovered_turns.append(recovery_key)
        state.data["recovered_turns"] = recovered_turns[-_RECOVERED_TURN_LIMIT:]


def _recover_assistant_transcript(paths: ProjectPaths, event: HookEvent) -> bool:
    """Atomically append missing client blocks and the hook's final reply row."""
    from compchem_memory.hook.client_transcripts import assistant_texts

    if not event.transcript_ref:
        return False
    agent_id = None if event.agent is None else event.agent.id
    recovered = assistant_texts(
        event.harness,
        event.transcript_ref,
        turn_id=event.turn_id,
        agent_id=agent_id,
    )
    if not recovered:
        return False
    candidates = _reply_candidates(event, recovered)
    _commit_recovered_replies(paths, event, agent_id, candidates)
    return True


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


def _boot_payload(paths: ProjectPaths) -> str | None:
    from compchem_memory.hook.boot_context import build_boot_payload

    return build_boot_payload(paths)


def _session_start(paths: ProjectPaths, event: HookEvent, guard: _Guard) -> Handled:
    response = HookResponse(context=guard("boot-context", _boot_payload, paths))
    response = response.merge(HookResponse(context=guard("directive", _directive, paths, event)))
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


def _doorbell(paths: ProjectPaths, event: HookEvent, env: Mapping[str, str]) -> str | None:
    from compchem_memory.hook.doorbell import process_prompt

    result = process_prompt(event.prompt or "", paths, env)
    if not result.sent:
        return None
    mentions = ", ".join(result.sent)
    return (
        "The following @mentions were already sent to helper agents; "
        f"do not act on them: {mentions}"
    )


def _prompt(paths: ProjectPaths, event: HookEvent, env: Mapping[str, str], guard: _Guard) -> Handled:
    if not is_person_prompt(event):
        return Handled(HookResponse(), None)
    response = HookResponse(context=guard("doorbell", _doorbell, paths, event, env))
    response = response.merge(HookResponse(context=guard("directive", _directive, paths, event)))
    notices = guard("notices", _notices, paths, event)
    response = response.merge(HookResponse(notice=None if notices is None else notices.text))
    return Handled(response, None, notices)


def _post_tool(paths: ProjectPaths, event: HookEvent, deadline: float, guard: _Guard) -> Handled:
    from compchem_memory.hook.action_retrieval import on_post_tool

    pending = guard("action-retrieval", on_post_tool, event, paths, deadline)
    response = HookResponse(tool_note=None if pending is None else pending.text)
    notices = guard("notices", _notices, paths, event) if event.agent is None else None
    response = response.merge(HookResponse(notice=None if notices is None else notices.text))
    return Handled(response, pending, notices)


def _turn_end(paths: ProjectPaths, event: HookEvent, guard: _Guard) -> Handled:
    from compchem_memory.hook.action_retrieval import probe_application
    from compchem_memory.hook.critic import run_critic

    guard(
        "application-probe", probe_application,
        paths, event.harness, event.session_id, event.assistant_text or "", event.turn_id,
    )
    agent_id = None if event.agent is None else event.agent.id
    guard("critic", run_critic, paths, event.harness, event.session_id, agent_id, event.turn_id)
    return Handled(HookResponse(), None)


def handle(event: HookEvent, env: Mapping[str, str], deadline: float) -> Handled:
    """Bind and record an event, then return its neutral hook response."""
    paths = bind_project(event.harness, event.session_id, Path(event.cwd), env)
    if paths is None:
        return Handled(HookResponse(), None)
    guard = _Guard(paths, event, env)
    guard("session-map", _record_session, paths, event)
    transcript_handled = False
    if event.event == "turn-end":
        transcript_handled = guard(
            "client-transcript", _recover_assistant_transcript, paths, event, default=False,
        )
    if not transcript_handled:
        guard("transcript", _append_transcript, paths, event)
    if event.event == "session-start":
        handled = _session_start(paths, event, guard)
    elif event.event == "prompt":
        handled = _prompt(paths, event, env, guard)
    elif event.event == "post-tool":
        handled = _post_tool(paths, event, deadline, guard)
    elif event.event == "turn-end":
        handled = _turn_end(paths, event, guard)
    else:
        handled = Handled(HookResponse(), None)
    handled.paths = paths
    return handled
