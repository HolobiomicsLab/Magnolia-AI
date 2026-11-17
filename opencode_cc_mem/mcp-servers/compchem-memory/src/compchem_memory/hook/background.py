"""Detached session worker for queued application probes and critic jobs."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState, private_mkdir, safe_component

_ATTEMPT_LIMIT = 3
_OPENCODE_LEASE_SECONDS = 300
_OPENCODE_DUMP_LIMIT = 4_000_000
_OPENCODE_PAYLOAD_MAX_AGE_SECONDS = 86_400


class _RejectPayload(ValueError):
    """A payload cannot ever resolve to durable work."""


class _ExpirePayload(RuntimeError):
    """A retryable payload exhausted its bounded attempts."""


def _mark(store: Path, harness: str, session_id: str, key: str, status: str) -> None:
    """Persist one turn's worker status."""
    with SessionState.open(store, harness, session_id) as state:
        ledger = state.data.get("background_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        row = ledger.get(key)
        row = dict(row) if isinstance(row, dict) else {}
        ledger[key] = {**row, "status": status}
        state.data["background_turns"] = ledger


def _update_pending_attempt(state: SessionState, job: dict, attempts: int) -> None:
    """Charge an attempt only when the worker claims the recovered row."""
    pending = state.data.get("catchup_pending")
    if not isinstance(pending, list):
        return
    for row in pending:
        if not isinstance(row, dict):
            continue
        if row.get("turn_id") == job.get("turn_id") \
                and row.get("agent_id") == job.get("agent_id"):
            row["attempts"] = attempts
            return


def _job_order(job: dict) -> tuple[int, float]:
    """Sort main turns first and children newest first."""
    return (0 if job.get("agent_id") is None else 1, -float(job.get("queued", 0)))


def _claim_next(store: Path, harness: str, session_id: str) -> dict | None:
    """Claim the next eligible job or mark the session worker idle."""
    with SessionState.open(store, harness, session_id) as state:
        queue = state.data.get("background_queue")
        queue = [row for row in queue if isinstance(row, dict)] if isinstance(queue, list) else []
        ledger = state.data.get("background_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        eligible = []
        for job in queue:
            current = ledger.get(job.get("key"))
            attempts = int(current.get("attempts", 0)) if isinstance(current, dict) else 0
            is_done = isinstance(current, dict) and current.get("status") == "done"
            if attempts < _ATTEMPT_LIMIT and not is_done:
                eligible.append(job)
        if not eligible:
            state.data["background_queue"] = []
            state.data["background_worker"] = {"status": "idle", "started": time.time()}
            return None
        job = min(eligible, key=_job_order)
        queue.remove(job)
        state.data["background_queue"] = queue
        current = ledger.get(job["key"])
        current = current if isinstance(current, dict) else {}
        attempts = int(current.get("attempts", 0)) + 1
        ledger[job["key"]] = {
            **current, "status": "running", "attempts": attempts, "started": time.time(),
        }
        state.data["background_turns"] = ledger
        _update_pending_attempt(state, job, attempts)
        return job


def _run_turn(paths: ProjectPaths, payload: dict, job: dict) -> None:
    """Run one queued turn under the existing per-turn lock."""
    store = paths.store
    harness = str(payload["harness"])
    session_id = str(payload["session_id"])
    key = str(job["key"])
    lock_path = store / "hook-workers" / f"{safe_component(key)}.lock"
    private_mkdir(lock_path.parent)
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _mark(store, harness, session_id, key, "failed")
            return
        try:
            from compchem_memory.hook.action_retrieval import probe_application
            from compchem_memory.hook.critic import run_critic

            probe_application(
                paths, harness, session_id, str(job.get("reply_text") or ""), job.get("turn_id"),
            )
            outcome = run_critic(
                paths, harness, session_id, job.get("agent_id"), job.get("turn_id"),
            )
            _mark(store, harness, session_id, key, (
                "failed" if getattr(outcome, "kind", "verdict") == "error" else "done"
            ))
        except Exception as error:  # noqa: BLE001 - worker failure is durable evidence
            from compchem_memory.hook.errors import log_error

            _mark(store, harness, session_id, key, "failed")
            log_error(error, harness, "background", store, {}, "catch-up-worker")


def _worker_lock(store: Path, harness: str, session_id: str) -> Path:
    """Return the one lock path shared by every worker for a session."""
    name = f"{safe_component(harness)}--{safe_component(session_id)}--worker.lock"
    return store / "hook-workers" / name


def run_job(job_path: Path) -> None:
    """Drain one session's queued jobs sequentially, then mark the worker idle."""
    job_path = Path(job_path)
    try:
        try:
            payload = json.loads(job_path.read_text(encoding="utf-8"))
            store = Path(payload["store"])
            harness = str(payload["harness"])
            session_id = str(payload["session_id"])
            paths = ProjectPaths(
                Path(payload["root"]), Path(payload["project_dir"]), store,
                str(payload["name"]), bool(payload["root_mode"]),
            )
        except (KeyError, OSError, ValueError, TypeError):
            return
        # No project writer lease: the probe and critic call an LLM and write
        # only self-locked streams (session state, verdict and surfacing logs).
        lock_path = _worker_lock(store, harness, session_id)
        private_mkdir(lock_path.parent)
        with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while (job := _claim_next(store, harness, session_id)) is not None:
                _run_turn(paths, payload, job)
    finally:
        job_path.unlink(missing_ok=True)


def _opencode_paths(payload: dict) -> ProjectPaths:
    """Restore project paths from a synchronous plugin handoff."""
    return ProjectPaths(
        Path(payload["root"]), Path(payload["project_dir"]), Path(payload["store"]),
        str(payload["name"]), bool(payload["root_mode"]),
    )


def _message_role(message: dict) -> str:
    """Return a role from either OpenCode message family."""
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    return str(info.get("role") or message.get("role") or message.get("type") or "").lower()


def _message_id(message: dict, index: int) -> str:
    """Return the durable message id, with a deterministic fallback."""
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    return str(info.get("id") or message.get("id") or f"message-{index}")


def _message_parts(message: dict) -> list[dict]:
    """Return text/tool parts from either OpenCode message family."""
    parts = message.get("parts")
    if not isinstance(parts, list):
        parts = message.get("content")
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []


def _reply_text(messages: list[dict], turn_id: str | None) -> str:
    """Collect assistant text after the selected user boundary."""
    active = turn_id is None
    parts: list[str] = []
    for index, message in enumerate(messages):
        if _message_role(message) == "user":
            active = _message_id(message, index) == turn_id
            if active:
                parts = []
            continue
        if not active or _message_role(message) != "assistant":
            continue
        for part in _message_parts(message):
            if part.get("type") == "text" and not part.get("synthetic") and part.get("text"):
                parts.append(str(part["text"]))
    return "\n".join(parts)


def _neutral_rows(messages: list[dict]) -> list[dict]:
    """Convert OpenCode messages to the hook-core transcript schema."""
    from compchem_memory.hook.transcript import _clean_input, clean_text

    rows: list[dict] = []
    current_turn: str | None = None
    for index, message in enumerate(messages):
        role = _message_role(message)
        if role == "user":
            current_turn = _message_id(message, index)
            text = "\n".join(
                str(part.get("text") or "") for part in _message_parts(message)
                if part.get("type") == "text"
            )
            rows.append({
                "schema_version": 1, "ts": "", "kind": "prompt",
                "turn_id": current_turn, "agent": None, "text": clean_text(text),
            })
            continue
        if role != "assistant":
            continue
        reply: list[str] = []
        for part in _message_parts(message):
            if part.get("type") == "text" and not part.get("synthetic"):
                if part.get("text"):
                    reply.append(str(part["text"]))
                continue
            if part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "")
            rows.append({
                "schema_version": 1, "ts": "", "kind": "tool", "phase": "post",
                "turn_id": current_turn, "agent": None,
                "name": str(part.get("tool") or part.get("name") or "unknown"),
                "server": None, "call_id": part.get("id") or part.get("callID"),
                "input": _clean_input(state.get("input") or part.get("input") or {}),
                "output": clean_text(str(state.get("output") or part.get("output") or ""), 2000),
                "is_error": status in {"error", "failed"} or bool(state.get("error")),
            })
        if reply:
            rows.append({
                "schema_version": 1, "ts": "", "kind": "assistant",
                "turn_id": current_turn, "agent": None, "text": clean_text("\n".join(reply)),
            })
    return rows


def _finalize_opencode_capture(
    paths: ProjectPaths, session_id: str, exported: dict,
) -> list[dict]:
    """Atomically persist the legacy dump and hook-core transcript."""
    from compchem_memory.atomic_io import atomic_write_text
    from compchem_memory.hook.transcript import transcript_path

    messages = exported.get("messages") if isinstance(exported, dict) else None
    if not isinstance(messages, list):
        raise ValueError("OpenCode export has no messages")
    messages = [message for message in messages if isinstance(message, dict)]
    dump = {
        "sessionID": session_id,
        "source": "detached-opencode-turn-worker",
        "messages": messages,
    }
    dump_text = json.dumps(dump, ensure_ascii=False)
    if len(dump_text) > _OPENCODE_DUMP_LIMIT:
        dump_text = json.dumps({
            "sessionID": session_id,
            "source": "detached-opencode-turn-worker",
            "truncated": True,
            "note": "session exceeded 4 MB; dump clipped — use the database fallback",
            "messages": messages[:200],
        }, ensure_ascii=False)
    rows = _neutral_rows(messages)
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    from compchem_memory.writer_lease import project_writer_lease

    with project_writer_lease(paths.project_dir):
        atomic_write_text(
            paths.store / "opencode-transcripts" / f"{session_id}.json",
            dump_text,
        )
        atomic_write_text(transcript_path(paths.store, "opencode", session_id), body)
    return messages


def _message_created(message: dict) -> float | None:
    """Return a message creation stamp in Unix seconds when available."""
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    time_data = info.get("time") if isinstance(info.get("time"), dict) else {}
    raw = time_data.get("created", info.get("time_created"))
    if raw is None:
        message_time = message.get("time") if isinstance(message.get("time"), dict) else {}
        raw = message_time.get("created", message.get("time_created"))
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _sequence_turn_id(payload: dict, exported: dict, user_ids: set[str]) -> str | None:
    """Resolve the v2 durable sequence boundary when its evidence is available."""
    durable_seq = payload.get("durable_seq")
    if not isinstance(durable_seq, (str, int, float)) or isinstance(durable_seq, bool):
        return None
    sequences = exported.get("turn_sequences")
    if isinstance(sequences, dict) and sequences.get(str(durable_seq)) in user_ids:
        return str(sequences[str(durable_seq)])
    users = exported.get("user_sequences")
    try:
        sequence_value = float(durable_seq)
    except (TypeError, ValueError):
        return None
    eligible = [
        row for row in users if isinstance(row, dict)
        and isinstance(row.get("seq"), (int, float))
        and float(row["seq"]) <= sequence_value
        and row.get("turn_id") in user_ids
    ] if isinstance(users, list) else []
    return str(max(eligible, key=lambda row: float(row["seq"]))["turn_id"]) \
        if eligible else None


def _message_turn_id(payload: dict, exported: dict, user_ids: set[str]) -> str | None:
    """Resolve an emitted message or event id to a real user boundary."""
    boundaries = exported.get("turn_boundaries")
    for identity in (payload.get("turn_id"), payload.get("event_id")):
        if isinstance(identity, str) and identity in user_ids:
            return identity
        if isinstance(identity, str) and isinstance(boundaries, dict) \
                and boundaries.get(identity) in user_ids:
            return str(boundaries[identity])
    return None


def _queued_turn_id(payload: dict, messages: list[dict]) -> str | None:
    """Resolve the last user message at the payload's queue-time boundary."""
    queued_at = payload.get("queued_at")
    if not isinstance(queued_at, (int, float)) or isinstance(queued_at, bool):
        return None
    eligible_messages = [
        (created, _message_id(message, index))
        for index, message in enumerate(messages)
        if _message_role(message) == "user"
        and (created := _message_created(message)) is not None
        and created <= float(queued_at)
    ]
    if eligible_messages:
        return max(eligible_messages, key=lambda row: row[0])[1]
    return None


def _payload_turn_id(payload: dict, exported: dict, messages: list[dict]) -> str | None:
    """Resolve durable sequence, message id, then queue-time boundary in order."""
    user_ids = {
        _message_id(message, index) for index, message in enumerate(messages)
        if _message_role(message) == "user"
    }
    return (
        _sequence_turn_id(payload, exported, user_ids)
        or _message_turn_id(payload, exported, user_ids)
        or _queued_turn_id(payload, messages)
    )


def _claim_opencode_operation(
    paths: ProjectPaths, session_id: str, operation: str, turn_id: str, owner: str,
) -> str:
    """Reserve one operation/turn, allowing its crashed owner to retry."""
    with SessionState.open(paths.store, "opencode", session_id) as state:
        ledger = state.data.get("opencode_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        key = f"{operation}--{turn_id}"
        row = ledger.get(key)
        row = row if isinstance(row, dict) else {}
        if row.get("status") == "done":
            return "done"
        is_live = row.get("status") == "running" and (
            time.time() - float(row.get("started", 0)) < _OPENCODE_LEASE_SECONDS
        )
        if is_live and row.get("owner") != owner:
            return "busy"
        ledger[key] = {
            **row, "status": "running", "owner": owner, "started": time.time(),
            "attempts": int(row.get("attempts", 0)) + 1,
        }
        state.data["opencode_turns"] = ledger
        return "claimed"


def _finish_opencode_operation(
    paths: ProjectPaths, session_id: str, operation: str, turn_id: str, status: str,
    reason: str | None = None,
) -> None:
    """Persist the terminal state of one OpenCode operation."""
    with SessionState.open(paths.store, "opencode", session_id) as state:
        ledger = state.data.get("opencode_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        key = f"{operation}--{turn_id}"
        row = ledger.get(key)
        ledger[key] = {
            **(row if isinstance(row, dict) else {}), "status": status,
            **({"reason": reason} if reason else {}),
        }
        state.data["opencode_turns"] = ledger


def _record_payload_error(job_path: Path, payload: dict, reason: str) -> None:
    """Persist one retryable failure with its payload across worker processes."""
    from compchem_memory.atomic_io import atomic_write_text

    if not job_path.exists():
        return
    payload["attempts"] = int(payload.get("attempts", 0)) + 1
    payload["last_error"] = reason
    atomic_write_text(job_path, json.dumps(payload, ensure_ascii=False))
    if payload["attempts"] >= _ATTEMPT_LIMIT:
        raise _ExpirePayload(reason)


def _run_opencode_payload(
    job_path: Path, payload: dict, critic_skip_reason: str | None = None,
) -> bool:
    """Finalize one payload; return whether its durable work is complete."""
    from compchem_memory import opencode_ingest

    paths = _opencode_paths(payload)
    session_id = str(payload["session_id"])
    exported = opencode_ingest.live_session_exporter()(session_id)
    if not isinstance(exported, dict):
        _record_payload_error(job_path, payload, "session export unavailable")
        return False
    messages = _finalize_opencode_capture(paths, session_id, exported)
    operation = str(payload["operation"])
    if operation == "capture":
        return True
    turn_id = _payload_turn_id(payload, exported, messages)
    if turn_id is None:
        raise _RejectPayload("unresolvable turn identity")
    owner = job_path.name
    claim = _claim_opencode_operation(paths, session_id, operation, turn_id, owner)
    if claim == "done":
        return True
    if claim == "busy":
        return False
    try:
        if operation == "critic":
            from compchem_memory.hook.critic import CriticOutcome, _has_verdict, run_critic

            outcome = CriticOutcome.skipped(critic_skip_reason) if critic_skip_reason else \
                run_critic(paths, "opencode", session_id, None, turn_id)
            verdict_path = paths.store / "claim-critic" / f"{safe_component(session_id)}.jsonl"
            if not isinstance(outcome, CriticOutcome):
                outcome = (
                    CriticOutcome.verdict()
                    if _has_verdict(verdict_path, "opencode", turn_id)
                    else CriticOutcome.error("critic returned no typed outcome")
                )
            if outcome.kind == "error":
                reason = outcome.reason or "critic error"
                _finish_opencode_operation(
                    paths, session_id, operation, turn_id, "failed", reason,
                )
                _record_payload_error(job_path, payload, reason)
                return False
            _finish_opencode_operation(
                paths, session_id, operation, turn_id, "done", outcome.reason,
            )
            return True
        elif operation == "probe":
            from compchem_memory.hook.action_retrieval import probe_items

            items = payload.get("items")
            owned = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
            settled = probe_items(
                paths, "opencode", session_id, owned, _reply_text(messages, turn_id), turn_id,
            )
            if len(settled) != len(owned):
                raise OSError("application telemetry could not be logged")
        elif operation != "capture":
            raise _RejectPayload(f"unknown OpenCode operation: {operation}")
    except (_RejectPayload, _ExpirePayload):
        raise
    except Exception as error:  # noqa: BLE001 - one payload must not abort the drain
        from compchem_memory.hook.errors import log_error

        _finish_opencode_operation(paths, session_id, operation, turn_id, "failed", str(error))
        log_error(error, "opencode", "background", paths.store, {}, "opencode-turn")
        _record_payload_error(job_path, payload, str(error))
        return False
    _finish_opencode_operation(paths, session_id, operation, turn_id, "done")
    return True


def _payload_age(path: Path, payload: dict) -> float:
    """Return payload age from its durable stamp or filesystem metadata."""
    stamp = payload.get("queued_at")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        stamp = path.stat().st_mtime
    return max(0.0, time.time() - float(stamp))


def _move_payload(path: Path, reason: str, category: str) -> None:
    """Move an unusable payload aside and append its explicit disposition."""
    destination_dir = path.parent / category
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / path.name
    if destination.exists():
        destination = destination.with_name(f"{destination.stem}-{time.time_ns()}.json")
    os.replace(path, destination)
    log_path = path.parent / "opencode-turn-errors.jsonl"
    with log_path.open("a", encoding="utf-8") as log:
        fcntl.flock(log.fileno(), fcntl.LOCK_EX)
        log.write(json.dumps({
            "ts": time.time(), "payload": str(destination), "reason": reason,
        }, ensure_ascii=False) + "\n")


def _read_opencode_payload(path: Path, store: Path) -> dict:
    """Load and validate one payload as belonging to this project store."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    required = ("store", "session_id", "operation", "root", "project_dir", "name")
    if any(not payload.get(field) for field in required):
        raise ValueError("payload is missing required fields")
    if Path(str(payload["store"])).resolve() != store.resolve():
        raise ValueError("payload belongs to another project store")
    if payload["operation"] not in {"capture", "critic", "probe"}:
        raise ValueError("payload has an unknown operation")
    for field in ("turn_id", "event_id"):
        if payload.get(field) is not None and not isinstance(payload.get(field), str):
            raise ValueError(f"payload {field} is not a string")
    attempts = payload.get("attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValueError("payload attempts is not a non-negative integer")
    return payload


def _peek_opencode_session_id(path: Path, store: Path) -> str:
    """Read only the routing envelope needed to select the session lock."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("session_id"):
        raise ValueError("payload is missing session id")
    if Path(str(payload.get("store", ""))).resolve() != store.resolve():
        raise ValueError("payload belongs to another project store")
    return str(payload["session_id"])


def drain_opencode_payloads(
    store: Path, *, critic_skip_reason: str | None = None,
) -> None:
    """Drain every pending OpenCode payload for a project, isolated by session."""
    store = Path(store)
    worker_dir = store / "hook-workers"
    if not worker_dir.exists():
        return
    attempted: set[Path] = set()
    while True:
        progress = False
        lock_acquired = False
        for candidate in sorted(worker_dir.glob("opencode-turn-*.json")):
            if candidate in attempted:
                continue
            payload = None
            try:
                session_id = _peek_opencode_session_id(candidate, store)
            except Exception as error:  # noqa: BLE001 - reject one and continue
                if candidate.exists():
                    try:
                        _move_payload(candidate, f"invalid payload: {error}", "rejected")
                        progress = True
                    except Exception:
                        attempted.add(candidate)
                continue
            try:
                lock_path = _worker_lock(store, "opencode", session_id)
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+") as lock:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    lock_acquired = True
                    attempted.add(candidate)
                    try:
                        payload = _read_opencode_payload(candidate, store)
                    except (TypeError, ValueError) as error:
                        _move_payload(candidate, f"invalid payload: {error}", "rejected")
                        progress = True
                        continue
                    if _payload_age(candidate, payload) > _OPENCODE_PAYLOAD_MAX_AGE_SECONDS:
                        _move_payload(
                            candidate, "payload exceeded 86400 second retry bound", "expired",
                        )
                        progress = True
                        continue
                    completed = (
                        _run_opencode_payload(candidate, payload, critic_skip_reason)
                        if critic_skip_reason else _run_opencode_payload(candidate, payload)
                    )
                    if completed:
                        candidate.unlink(missing_ok=True)
                        progress = True
            except _RejectPayload as error:
                try:
                    _move_payload(candidate, str(error), "rejected")
                    progress = True
                except Exception:
                    attempted.add(candidate)
            except _ExpirePayload as error:
                try:
                    _move_payload(
                        candidate,
                        f"payload exhausted {_ATTEMPT_LIMIT} attempts: {error}",
                        "expired",
                    )
                    progress = True
                except Exception:
                    attempted.add(candidate)
            except Exception as error:  # noqa: BLE001 - leave retryable and continue
                attempted.add(candidate)
                try:
                    paths = _opencode_paths(payload) if isinstance(payload, dict) else None
                    from compchem_memory.hook.errors import log_error

                    if paths is not None:
                        log_error(
                            error, "opencode", "background", paths.store, {}, "opencode-drain",
                        )
                except Exception:
                    pass
        if not progress and not lock_acquired:
            return


def run_opencode_job(job_path: Path) -> None:
    """Use one handoff to drain every pending OpenCode payload for its project."""
    job_path = Path(job_path)
    store = job_path.parent.parent
    try:
        payload = json.loads(job_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("store"):
            store = Path(str(payload["store"]))
    except (OSError, ValueError, TypeError):
        pass
    # The export (a subprocess) and the critic (an LLM) run outside the lease;
    # only the capture write in _finalize_opencode_capture takes it.
    drain_opencode_payloads(store)


def main() -> None:
    """Run the job path supplied by the spawning hook."""
    if len(sys.argv) == 3 and sys.argv[1] == "--opencode-turn":
        run_opencode_job(Path(sys.argv[2]))
    elif len(sys.argv) == 2:
        run_job(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
