"""Route neutral hook events through Magnolia's portable behaviours.

Claude, Codex and Vibe turn-end events recover replies and launch a detached critic
worker, keeping synchronous client hooks short.

Behaviours are isolated from each other, as the OpenCode plugins are: each runs
under its own guard, a failure leaves one row in ``hook-errors.jsonl`` (in the
store, or in Magnolia's state home when the store is unwritable) and the other
behaviours still run, so a failing transcript or directive ledger never costs
the boot context.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from compchem_memory.hook.events import AgentRef, HookEvent, HookResponse
from compchem_memory.hook.project import bind_project

if TYPE_CHECKING:
    from compchem_memory.hook.action_retrieval import ActionNote
    from compchem_memory.hook.notices import Notices
    from compchem_memory.hook.project import ProjectPaths

_RECOVERED_TURN_LIMIT = 200
_VIBE_TURN_LIMIT = 200
_CATCHUP_ATTEMPT_LIMIT = 3
_CATCHUP_MAX_AGE_SECONDS = 24 * 60 * 60
_CATCHUP_JOURNAL_LIMIT = 16
_CATCHUP_HOOK_LIMIT_SECONDS = 4.0
_CATCHUP_EXIT_MARGIN_SECONDS = 0.5
_WORKER_LEASE_SECONDS = 300
_QUEUE_ORDER_STEP_SECONDS = 0.000001
_CARRIED_RESERVATION_TTL_SECONDS = 60
# Claude Code shows its model only about 2 KB of SessionStart context.
CLAUDE_CONTEXT_LIMIT = 1800


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
    carried: list["CarriedNote"] = field(default_factory=list)
    carried_field: str | None = None
    injections: list["Injection"] = field(default_factory=list)


@dataclass
class CarriedNote:
    """One reserved Vibe action note waiting for a deliverable event."""

    id: str
    text: str
    call_id: str | None
    note: ActionNote


@dataclass(frozen=True)
class Injection:
    """One response fragment to record only after client delivery."""

    field: str
    channel: str
    text: str
    entry_ids: list[str]
    tool_call_id: str | None


class _Guard:
    """Run one behaviour; log its failure and return a default instead of raising."""

    def __init__(self, paths: ProjectPaths, event: HookEvent, env: Mapping[str, str]) -> None:
        self.paths = paths
        self.event = event
        self.env = env

    def __call__(
        self,
        behaviour: str,
        function: Callable[..., Any],
        *args: Any,
        default: Any = None,
        **kwargs: Any,
    ) -> Any:
        try:
            return function(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - one behaviour must not stop the others
            from compchem_memory.hook.errors import log_error

            log_error(error, self.event.harness, self.event.event, self.paths.store, self.env, behaviour)
            return default


def _record_session(paths: ProjectPaths, event: HookEvent) -> None:
    from compchem_memory.hook.sessions import record_session

    record_session(
        paths.store, event.harness, event.session_id, event.cwd, event.transcript_ref,
        model=event.model, role=event.role,
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
    fills_native_reply = event.harness == "vibe" or event.event == "agent-end"
    if fills_native_reply and event.assistant_text is None:
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


def _boot_payload(paths: ProjectPaths, harness: str = "json") -> str | None:
    from compchem_memory.hook.boot_context import build_boot_payload, build_claude_boot_payload

    if harness in {"claude", "codex"}:
        return build_claude_boot_payload(paths, CLAUDE_CONTEXT_LIMIT)
    return build_boot_payload(
        paths, include_agents_md=harness == "vibe",
        harness=harness if harness == "vibe" else None,
    )


def _session_start(paths: ProjectPaths, event: HookEvent, guard: _Guard) -> Handled:
    response = HookResponse(context=guard("boot-context", _boot_payload, paths, event.harness))
    response = response.merge(HookResponse(context=guard("directive", _directive, paths, event)))
    injections = [Injection("context", "boot", response.context, [], None)] \
        if response.context else []
    return Handled(response, None, injections=injections)


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
        "One letter containing all requests was sent for each of these helper agents; "
        f"do not act on them: {mentions}"
    )


def _prompt(paths: ProjectPaths, event: HookEvent, env: Mapping[str, str], guard: _Guard) -> Handled:
    if not is_person_prompt(event):
        return Handled(HookResponse(), None)
    carried = guard("carried-context", _carried_notes, paths, event, default=[])
    carried_text = "\n\n".join(item.text for item in carried) or None
    response = HookResponse(context=carried_text)
    doorbell = guard("doorbell", _doorbell, paths, event, env)
    response = response.merge(HookResponse(context=doorbell))
    response = response.merge(HookResponse(context=guard("directive", _directive, paths, event)))
    notices = guard("notices", _notices, paths, event)
    response = response.merge(HookResponse(notice=None if notices is None else notices.text))
    injections = [
        Injection("context", "action_memory", item.text,
                  _note_entry_ids(item.note), item.call_id)
        for item in carried
    ]
    if doorbell:
        injections.append(Injection("context", "doorbell", doorbell, [], None))
    if notices is not None:
        injections.append(Injection("notice", "notice", notices.text, [], None))
    return Handled(
        response, None, notices, carried=carried, carried_field="context",
        injections=injections,
    )


def _post_tool(paths: ProjectPaths, event: HookEvent, deadline: float, guard: _Guard) -> Handled:
    from compchem_memory.hook.action_retrieval import on_post_tool

    pending = guard("action-retrieval", on_post_tool, event, paths, deadline)
    is_builtin = event.tool is not None and event.tool.server is None
    carried = guard("carried-context", _carried_notes, paths, event, default=[]) \
        if is_builtin else []
    notes = [item.text for item in carried]
    if pending is not None:
        notes.append(pending.text)
    response = HookResponse(tool_note="\n\n".join(notes) or None)
    notices = guard("notices", _notices, paths, event) if event.agent is None else None
    response = response.merge(HookResponse(notice=None if notices is None else notices.text))
    injections = [
        Injection("tool_note", "action_memory", item.text,
                  _note_entry_ids(item.note), item.call_id)
        for item in carried
    ]
    if pending is not None:
        call_id = event.tool.call_id if event.tool is not None else None
        injections.append(Injection(
            "tool_note", "action_memory", pending.text, _note_entry_ids(pending), call_id,
        ))
    if notices is not None:
        injections.append(Injection("notice", "notice", notices.text, [], None))
    return Handled(
        response, pending, notices, carried=carried, carried_field="tool_note",
        injections=injections,
    )


def _note_entry_ids(note: ActionNote) -> list[str]:
    from compchem_memory.hook.action_retrieval import note_entry_ids

    return note_entry_ids(note)


def _carried_notes(paths: ProjectPaths, event: HookEvent) -> list[CarriedNote]:
    """Reserve this agent's Vibe notes before rendered delivery."""
    if event.harness != "vibe":
        return []
    from compchem_memory.hook.action_retrieval import restore_note
    from compchem_memory.hook.state import SessionState

    agent_id = None if event.agent is None else event.agent.id
    selected: list[tuple[dict, ActionNote]] = []
    now = time.time()
    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        queued = state.data.get("carried_action_notes")
        queued = [item for item in queued if isinstance(item, dict)] \
            if isinstance(queued, list) else []
        in_flight = state.data.get("carried_action_notes_in_flight")
        in_flight = in_flight if isinstance(in_flight, dict) else {}
        for note_id, reservation in list(in_flight.items()):
            if not isinstance(reservation, dict):
                in_flight.pop(note_id, None)
                continue
            if now - float(reservation.get("ts", now)) > _CARRIED_RESERVATION_TTL_SECONDS:
                payload = reservation.get("payload")
                if isinstance(payload, dict):
                    queued.insert(0, payload)
                in_flight.pop(note_id, None)
        remaining = []
        for payload in queued:
            note = restore_note(payload.get("note", {}), paths)
            if note is None:
                continue
            if payload.get("agent_id") != agent_id:
                remaining.append(payload)
                continue
            note_id = str(payload.get("id") or "")
            selected.append((payload, note))
            in_flight[note_id] = {"ts": now, "payload": payload}
        state.data["carried_action_notes"] = remaining
        state.data["carried_action_notes_in_flight"] = in_flight
    carried = []
    for payload, note in selected:
        tool = str(payload.get("tool") or "tool")
        carried.append(CarriedNote(
            id=str(payload.get("id") or ""),
            text=f"After your earlier `{tool}` call: {note.text}",
            call_id=payload.get("call_id"),
            note=note,
        ))
    return carried


def stash_carried_note(paths: ProjectPaths, event: HookEvent, note: ActionNote) -> None:
    """Persist an undeliverable Vibe MCP note with its original reservation."""
    import uuid
    from compchem_memory.hook.action_retrieval import discard_note, restore_note, serialise_note
    from compchem_memory.hook.state import SessionState

    tool = event.tool.name if event.tool is not None else "tool"
    call_id = event.tool.call_id if event.tool is not None else None
    payload = {
        "id": uuid.uuid4().hex, "tool": tool, "call_id": call_id,
        "agent_id": None if event.agent is None else event.agent.id,
        "note": serialise_note(note),
    }
    dropped = []
    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        queued = state.data.get("carried_action_notes")
        queued = queued if isinstance(queued, list) else []
        combined = [*queued, payload]
        dropped = combined[:-_VIBE_TURN_LIMIT]
        state.data["carried_action_notes"] = combined[-_VIBE_TURN_LIMIT:]
    for old_payload in dropped:
        old_note = restore_note(old_payload.get("note", {}), paths) \
            if isinstance(old_payload, dict) else None
        if old_note is not None:
            discard_note(old_note, reason="carried-note queue limit exceeded")


def clear_carried_notes(paths: ProjectPaths, event: HookEvent, ids: list[str]) -> None:
    """Commit only the carried-note reservations that were delivered."""
    from compchem_memory.hook.state import SessionState

    selected = set(ids)
    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        queued = state.data.get("carried_action_notes")
        if isinstance(queued, list):
            state.data["carried_action_notes"] = [
                item for item in queued
                if not isinstance(item, dict) or item.get("id") not in selected
            ]
        in_flight = state.data.get("carried_action_notes_in_flight")
        if isinstance(in_flight, dict):
            for note_id in selected:
                in_flight.pop(note_id, None)


def release_carried_notes(paths: ProjectPaths, event: HookEvent, ids: list[str]) -> None:
    """Return undelivered carried-note reservations to the queue."""
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        queued = state.data.get("carried_action_notes")
        queued = [item for item in queued if isinstance(item, dict)] \
            if isinstance(queued, list) else []
        in_flight = state.data.get("carried_action_notes_in_flight")
        if not isinstance(in_flight, dict):
            return
        restored = []
        for note_id in ids:
            reservation = in_flight.pop(note_id, None)
            payload = reservation.get("payload") if isinstance(reservation, dict) else None
            if isinstance(payload, dict):
                restored.append(payload)
        state.data["carried_action_notes"] = [*restored, *queued]


def _drop_child_carried_notes(paths: ProjectPaths, event: HookEvent) -> None:
    """Release queued notes that cannot outlive their Vibe child agent."""
    if event.harness != "vibe" or event.agent is None:
        return
    from compchem_memory.hook.action_retrieval import discard_note, restore_note
    from compchem_memory.hook.state import SessionState

    dropped = []
    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        queued = state.data.get("carried_action_notes")
        queued = [item for item in queued if isinstance(item, dict)] \
            if isinstance(queued, list) else []
        dropped = [item for item in queued if item.get("agent_id") == event.agent.id]
        state.data["carried_action_notes"] = [
            item for item in queued if item.get("agent_id") != event.agent.id
        ]
        in_flight = state.data.get("carried_action_notes_in_flight")
        in_flight = in_flight if isinstance(in_flight, dict) else {}
        for note_id, reservation in list(in_flight.items()):
            payload = reservation.get("payload") if isinstance(reservation, dict) else None
            if isinstance(payload, dict) and payload.get("agent_id") == event.agent.id:
                dropped.append(payload)
                in_flight.pop(note_id, None)
        state.data["carried_action_notes_in_flight"] = in_flight
    for payload in dropped:
        note = restore_note(payload.get("note", {}), paths)
        if note is not None:
            discard_note(note, reason="child ended before a deliverable post-tool event")


def _drop_session_carried_notes(paths: ProjectPaths, event: HookEvent) -> None:
    """Discard every undelivered Vibe reservation when its session closes."""
    if event.harness != "vibe":
        return
    from compchem_memory.hook.action_retrieval import discard_note, restore_note
    from compchem_memory.hook.state import SessionState

    dropped = []
    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        queued = state.data.get("carried_action_notes")
        if isinstance(queued, list):
            dropped.extend(item for item in queued if isinstance(item, dict))
        in_flight = state.data.get("carried_action_notes_in_flight")
        if isinstance(in_flight, dict):
            dropped.extend(
                reservation["payload"] for reservation in in_flight.values()
                if isinstance(reservation, dict) and isinstance(reservation.get("payload"), dict)
            )
        state.data["carried_action_notes"] = []
        state.data["carried_action_notes_in_flight"] = {}
    for payload in dropped:
        note = restore_note(payload.get("note", {}), paths)
        if note is not None:
            discard_note(note, reason="session ended before a deliverable event")


def _turn_end(paths: ProjectPaths, event: HookEvent, guard: _Guard) -> Handled:
    if event.harness in {"claude", "vibe", "codex"}:
        reply = event.assistant_text or _reply_for_turn(paths, event)
        guard("background-critic", _schedule_background, paths, event, reply)
        return Handled(HookResponse(), None)
    from compchem_memory.hook.action_retrieval import probe_application
    from compchem_memory.hook.critic import run_critic

    guard(
        "application-probe", probe_application,
        paths, event.harness, event.session_id, event.assistant_text or "", event.turn_id,
    )
    agent_id = None if event.agent is None else event.agent.id
    guard("critic", run_critic, paths, event.harness, event.session_id, agent_id, event.turn_id)
    return Handled(HookResponse(), None)


def _turn_key(event: HookEvent) -> str | None:
    if not event.turn_id:
        return None
    agent_id = "main" if event.agent is None else event.agent.id
    return f"{agent_id}--{event.turn_id}"


def _background_queue_row(event: HookEvent, reply_text: str) -> dict | None:
    """Build one durable probe/critic queue row."""
    if (key := _turn_key(event)) is None:
        return None
    return {
        "key": key,
        "agent_id": None if event.agent is None else event.agent.id,
        "turn_id": event.turn_id,
        "reply_text": reply_text,
        "queued": time.time(),
    }


def _has_live_lease(row: object) -> bool:
    """Return whether a persisted worker row still owns its lease."""
    return isinstance(row, dict) and row.get("status") == "running" and (
        time.time() - float(row.get("started", time.time())) < _WORKER_LEASE_SECONDS
    )


def _trim_background_queue(queue: list[dict]) -> list[dict]:
    """Keep every main job before retaining the newest child jobs."""
    main = [row for row in queue if row.get("agent_id") is None]
    children = [row for row in queue if row.get("agent_id") is not None]
    if len(main) >= _VIBE_TURN_LIMIT:
        return main
    child_limit = _VIBE_TURN_LIMIT - len(main)
    return [*main, *children[-child_limit:]]


def _reserve_session_worker(
    paths: ProjectPaths, event: HookEvent, rows: list[dict],
) -> tuple[bool, bool]:
    """Enqueue new rows and reserve the session's sole worker when needed."""
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        queue = state.data.get("background_queue")
        queue = [row for row in queue if isinstance(row, dict)] if isinstance(queue, list) else []
        ledger = state.data.get("background_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        queued_keys = {row.get("key") for row in queue}
        added = False
        for row in rows:
            key = row["key"]
            current = ledger.get(key)
            status = current.get("status") if isinstance(current, dict) else None
            if status == "done" or key in queued_keys or _has_live_lease(current):
                continue
            queue.append(row)
            queued_keys.add(key)
            attempts = int(current.get("attempts", 0)) if isinstance(current, dict) else 0
            ledger[key] = {"status": "queued", "attempts": attempts}
            added = True
        trimmed_queue = _trim_background_queue(queue)
        retained_keys = {row.get("key") for row in trimmed_queue}
        for key, current in ledger.items():
            if key not in retained_keys and isinstance(current, dict) \
                    and current.get("status") == "queued":
                current["status"] = "failed"
        state.data["background_queue"] = trimmed_queue
        state.data["background_turns"] = dict(list(ledger.items())[-_VIBE_TURN_LIMIT:])
        worker = state.data.get("background_worker")
        is_running = _has_live_lease(worker)
        should_spawn = bool(trimmed_queue) and not is_running
        if should_spawn:
            state.data["background_worker"] = {"status": "running", "started": time.time()}
        return added, should_spawn


def _worker_payload(paths: ProjectPaths, event: HookEvent) -> dict:
    """Return the session metadata needed by the queue-draining worker."""
    return {
        "root": str(paths.root), "project_dir": str(paths.project_dir),
        "store": str(paths.store), "name": paths.name, "root_mode": paths.root_mode,
        "harness": event.harness, "session_id": event.session_id,
    }


def _mark_worker_spawn_failed(paths: ProjectPaths, event: HookEvent) -> None:
    """Release a failed worker reservation without dropping queued jobs."""
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        state.data["background_worker"] = {"status": "failed", "started": time.time()}


def _schedule_background_batch(
    paths: ProjectPaths, event: HookEvent, jobs: list[tuple[HookEvent, str]],
) -> bool:
    """Enqueue jobs and spawn at most one detached worker for this session."""
    from compchem_memory.atomic_io import atomic_write_text
    from compchem_memory.hook.state import private_mkdir

    queued_at = time.time()
    rows = [row for job, reply in jobs if (row := _background_queue_row(job, reply))]
    for index, row in enumerate(rows):
        row["queued"] = queued_at - index * _QUEUE_ORDER_STEP_SECONDS
    added, should_spawn = _reserve_session_worker(paths, event, rows)
    if not should_spawn:
        return added
    folder = paths.store / "hook-workers"
    private_mkdir(folder)
    job_path = folder / f"session-worker-{time.time_ns()}.json"
    atomic_write_text(job_path, json.dumps(_worker_payload(paths, event), ensure_ascii=False))
    try:
        runner = Path(__file__).with_name("runner.py")
        subprocess.Popen(
            [sys.executable, "-I", str(runner), "--background", str(job_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return added
    except Exception as error:  # noqa: BLE001 - caller remains fail-open
        job_path.unlink(missing_ok=True)
        _mark_worker_spawn_failed(paths, event)
        from compchem_memory.hook.errors import log_error

        log_error(error, event.harness, event.event, paths.store, {}, "catch-up-worker")
        return False


def _schedule_background(paths: ProjectPaths, event: HookEvent, reply_text: str) -> bool:
    """Enqueue one turn for the session's detached queue-draining worker."""
    return _schedule_background_batch(paths, event, [(event, reply_text)])


def _defer_turn(paths: ProjectPaths, event: HookEvent) -> None:
    """Persist one client-journal turn for bounded later recovery."""
    if not event.transcript_ref:
        return
    from compchem_memory.hook.state import SessionState

    agent_id = None if event.agent is None else event.agent.id
    visible_turns: list[str] = []
    if event.harness == "vibe" and not event.turn_id:
        from compchem_memory.hook.client_transcripts import vibe_turns

        visible_turns = vibe_turns(event.transcript_ref, agent_id=agent_id)
    resolved_turn = event.turn_id or (visible_turns[-1] if visible_turns else None)
    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        sequence = int(state.data.get("catchup_sequence", 0)) + 1
        state.data["catchup_sequence"] = sequence
        agent_key = agent_id or "main"
        counts = state.data.get("catchup_agent_turn_counts")
        counts = counts if isinstance(counts, dict) else {}
        turn_index = int(counts.get(agent_key, 0))
        counts[agent_key] = turn_index + 1
        state.data["catchup_agent_turn_counts"] = counts
        pending = state.data.get("catchup_pending")
        pending = [row for row in pending if isinstance(row, dict)] \
            if isinstance(pending, list) else []
        same_journal = [
            row for row in pending
            if row.get("agent_id") == agent_id
            and row.get("transcript_ref") == event.transcript_ref
        ]
        candidate_index = visible_turns.index(resolved_turn) \
            if resolved_turn in visible_turns else None
        is_reserved = any(
            row.get("turn_id") == resolved_turn
            or (candidate_index is not None and row.get("turn_index") == candidate_index)
            for row in same_journal
        )
        if not event.turn_id and (is_reserved or candidate_index != turn_index):
            resolved_turn = None
        identifier = (
            f"{agent_key}--{resolved_turn}"
            if resolved_turn else f"{agent_key}--pending-{sequence}"
        )
        item = {
            "id": identifier, "turn_id": resolved_turn, "transcript_ref": event.transcript_ref,
            "agent_id": agent_id,
            "agent_type": None if event.agent is None else event.agent.type,
            "created": time.time(), "attempts": 0, "turn_index": turn_index,
        }
        pending = [row for row in pending if row.get("id") != identifier]
        state.data["catchup_pending"] = [*pending, item][-_VIBE_TURN_LIMIT:]
    if event.harness == "vibe" and resolved_turn and event.agent is not None:
        from compchem_memory.hook.transcript import append_turn_boundary

        append_turn_boundary(paths.store, replace(event, turn_id=resolved_turn))


def _pending_turns(paths: ProjectPaths, event: HookEvent) -> list[dict]:
    """Prioritise the newest main turn, then newest idle child turns."""
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        pending = state.data.get("catchup_pending")
        pending = [row for row in pending if isinstance(row, dict)] \
            if isinstance(pending, list) else []
        ledger = state.data.get("background_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        now = time.time()
        kept = []
        available = []
        for row in pending:
            turn_id = row.get("turn_id")
            agent_id = row.get("agent_id") or "main"
            key = f"{agent_id}--{turn_id}" if turn_id else None
            status = ledger.get(key, {}).get("status") if key and isinstance(ledger.get(key), dict) else None
            expired = now - float(row.get("created", now)) >= _CATCHUP_MAX_AGE_SECONDS
            exhausted = int(row.get("attempts", 0)) >= _CATCHUP_ATTEMPT_LIMIT
            if status == "done" or expired or exhausted:
                continue
            kept.append(row)
            current = ledger.get(key) if key else None
            is_running = status in {"queued", "running"} and isinstance(current, dict) and (
                status == "queued" or
                now - float(current.get("started", now)) < _WORKER_LEASE_SECONDS
            )
            if not is_running:
                available.append(row)
        state.data["catchup_pending"] = kept
        def newest(rows: list[dict]) -> list[dict]:
            return [row for _, row in sorted(
                enumerate(rows),
                key=lambda item: (float(item[1].get("created", 0)), item[0]),
                reverse=True,
            )]

        main = newest([row for row in available if row.get("agent_id") is None])
        children = newest([row for row in available if row.get("agent_id") is not None])
        return [*main, *children]


def _update_pending(
    paths: ProjectPaths,
    event: HookEvent,
    item: dict,
    *,
    turn_id: str | None,
    recovered: bool,
) -> None:
    """Record recovery without charging an attempt before judging starts."""
    from compchem_memory.hook.state import SessionState

    with SessionState.open(paths.store, event.harness, event.session_id) as state:
        pending = state.data.get("catchup_pending")
        if not isinstance(pending, list):
            return
        for row in pending:
            if isinstance(row, dict) and row.get("id") == item.get("id"):
                row["turn_id"] = turn_id
                row["recovered"] = recovered
                if not recovered:
                    row["attempts"] = int(row.get("attempts", 0)) + 1
                break


def _reply_for_turn(paths: ProjectPaths, event: HookEvent) -> str:
    """Return the last recovered assistant block for one agent and turn."""
    from compchem_memory.hook.transcript import read_turns, transcript_path

    agent_id = None if event.agent is None else event.agent.id
    replies = []
    for row in read_turns(transcript_path(paths.store, event.harness, event.session_id)):
        row_agent = row.get("agent")
        row_agent_id = row_agent.get("id") if isinstance(row_agent, dict) else None
        if row.get("kind") == "assistant" and row.get("turn_id") == event.turn_id \
                and row_agent_id == agent_id:
            replies.append(str(row.get("text") or ""))
    return replies[-1] if replies else ""


def _catch_up_pending(
    paths: ProjectPaths, event: HookEvent, guard: _Guard, deadline: float,
) -> None:
    """Recover up to 16 journals, then enqueue one ordered worker batch."""
    if event.agent is not None:
        return
    from compchem_memory.hook.client_transcripts import vibe_assistant_turns

    stop_at = deadline - _CATCHUP_EXIT_MARGIN_SECONDS
    ready: list[tuple[HookEvent, str]] = []
    journals_read = 0
    for item in _pending_turns(paths, event):
        turn_id = item.get("turn_id")
        agent_id = item.get("agent_id")
        transcript_ref = item.get("transcript_ref")
        agent = AgentRef(str(agent_id), item.get("agent_type")) if agent_id else None
        catch_up = replace(
            event, event="turn-end", turn_id=turn_id, agent=agent, assistant_text=None,
            transcript_ref=str(transcript_ref),
        )
        if item.get("recovered") and turn_id:
            ready.append((catch_up, _reply_for_turn(paths, catch_up)))
            continue
        if journals_read >= _CATCHUP_JOURNAL_LIMIT or time.monotonic() >= stop_at:
            break
        journals_read += 1
        if not turn_id and event.harness == "vibe":
            turns = vibe_assistant_turns(str(transcript_ref), agent_id=agent_id)
            turn_index = item.get("turn_index")
            if isinstance(turn_index, int) and 0 <= turn_index < len(turns):
                turn_id = turns[turn_index]
        catch_up = replace(
            event, event="turn-end", turn_id=turn_id, agent=agent, assistant_text=None,
            transcript_ref=str(transcript_ref),
        )
        recovered = guard(
            "client-transcript", _recover_assistant_transcript,
            paths, catch_up, default=False,
        )
        _update_pending(paths, event, item, turn_id=turn_id, recovered=bool(recovered))
        if not recovered or not turn_id:
            continue
        ready.append((catch_up, _reply_for_turn(paths, catch_up)))
    _schedule_background_batch(paths, event, ready)


def _last_prompt_turn(paths: ProjectPaths, event: HookEvent) -> str | None:
    """Return the turn id of this agent's last recorded prompt.

    Vibe's ``post_agent`` payload carries no turn id; its ``pre_agent`` payload does.
    """
    from compchem_memory.hook.transcript import read_turns, transcript_path

    agent_id = None if event.agent is None else event.agent.id
    for row in reversed(read_turns(transcript_path(paths.store, event.harness, event.session_id))):
        row_agent = row.get("agent")
        row_agent_id = row_agent.get("id") if isinstance(row_agent, dict) else None
        if row.get("kind") == "prompt" and row_agent_id == agent_id:
            turn_id = row.get("turn_id")
            return turn_id if isinstance(turn_id, str) and turn_id else None
    return None


def handle(event: HookEvent, env: Mapping[str, str], deadline: float) -> Handled:
    """Bind and record an event, then return its neutral hook response.

    No hook event takes the project writer lease, so none can wait on a long
    holder and a required pre-tool hook never denies because of it. A hook
    writes only its own self-locked streams (session map, transcript, session
    state, notice reservations, surfacing and doorbell appends); writes to the
    leased store happen in the detached background worker, which leases each
    short write itself.
    """
    paths = bind_project(event.harness, event.session_id, Path(event.cwd), env)
    if paths is None:
        return Handled(HookResponse(), None)
    return _handle_bound(paths, event, env, deadline)


def _handle_bound(
    paths: ProjectPaths, event: HookEvent, env: Mapping[str, str], deadline: float
) -> Handled:
    """Handle one already-bound event."""
    catchup_deadline = min(deadline, time.monotonic() + _CATCHUP_HOOK_LIMIT_SECONDS)
    guard = _Guard(paths, event, env)
    completion_event = event.event in {"turn-end", "agent-end"}
    closing_session_end = event.event == "session-end" and event.harness in {"vibe", "claude"}
    if completion_event and not event.turn_id:
        turn_id = guard("turn-id", _last_prompt_turn, paths, event, default=None)
        if not turn_id and event.harness == "vibe" and event.transcript_ref \
                and event.agent is None:
            from compchem_memory.hook.client_transcripts import latest_vibe_assistant_turn

            agent_id = None if event.agent is None else event.agent.id
            turn_id = guard(
                "turn-id", latest_vibe_assistant_turn,
                event.transcript_ref, agent_id=agent_id, default=None,
            )
        if turn_id:
            event = replace(event, turn_id=turn_id)
    guard("session-map", _record_session, paths, event)
    if event.event == "prompt" and event.harness == "vibe" and event.agent is None:
        guard("catch-up", _catch_up_pending, paths, event, guard, catchup_deadline)
    transcript_handled = False
    if completion_event:
        transcript_handled = guard(
            "client-transcript", _recover_assistant_transcript, paths, event, default=False,
        )
    if (not transcript_handled or event.event == "agent-end") and not closing_session_end:
        guard("transcript", _append_transcript, paths, event)
    if completion_event and event.agent is not None:
        guard("carried-context", _drop_child_carried_notes, paths, event)
    if event.event == "session-end" and event.harness == "vibe" and event.agent is None:
        guard("carried-context", _drop_session_carried_notes, paths, event)
    if event.event == "session-start":
        handled = _session_start(paths, event, guard)
    elif event.event == "prompt":
        handled = _prompt(paths, event, env, guard)
    elif event.event == "post-tool":
        handled = _post_tool(paths, event, deadline, guard)
    elif completion_event:
        if event.harness in {"vibe", "claude"}:
            guard("catch-up-ledger", _defer_turn, paths, event)
            handled = Handled(HookResponse(), None)
        else:
            handled = _turn_end(paths, event, guard)
        if event.harness == "claude" and event.event == "turn-end":
            guard("catch-up", _catch_up_pending, paths, event, guard, catchup_deadline)
    elif event.event == "session-end" and event.harness in {"vibe", "claude"}:
        guard("catch-up", _catch_up_pending, paths, event, guard, catchup_deadline)
        handled = Handled(HookResponse(), None)
    else:
        handled = Handled(HookResponse(), None)
    if closing_session_end and event.agent is None:
        guard("transcript", _append_transcript, paths, event)
    handled.paths = paths
    return handled
