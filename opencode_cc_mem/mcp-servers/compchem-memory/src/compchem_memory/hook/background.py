"""Detached session worker for queued application probes and critic jobs."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState, private_mkdir, safe_component

_ATTEMPT_LIMIT = 3
_OPENCODE_LEASE_SECONDS = 300
_OPENCODE_DUMP_LIMIT = 4_000_000
_OPENCODE_PAYLOAD_MAX_AGE_SECONDS = 86_400


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
            run_critic(paths, harness, session_id, job.get("agent_id"), job.get("turn_id"))
            _mark(store, harness, session_id, key, "done")
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
    atomic_write_text(
        paths.store / "opencode-transcripts" / f"{session_id}.json",
        dump_text,
    )
    rows = _neutral_rows(messages)
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    atomic_write_text(transcript_path(paths.store, "opencode", session_id), body)
    return messages


def _payload_turn_id(payload: dict, exported: dict, messages: list[dict]) -> str | None:
    """Resolve only the terminal event's own turn; never guess the newest turn."""
    identity = payload.get("turn_id")
    if not isinstance(identity, str) or not identity:
        return None
    user_ids = {
        _message_id(message, index) for index, message in enumerate(messages)
        if _message_role(message) == "user"
    }
    if identity in user_ids:
        return identity
    boundaries = exported.get("turn_boundaries")
    if isinstance(boundaries, dict) and boundaries.get(identity) in user_ids:
        return str(boundaries[identity])
    return None


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
) -> None:
    """Persist the terminal state of one OpenCode operation."""
    with SessionState.open(paths.store, "opencode", session_id) as state:
        ledger = state.data.get("opencode_turns")
        ledger = ledger if isinstance(ledger, dict) else {}
        key = f"{operation}--{turn_id}"
        row = ledger.get(key)
        ledger[key] = {**(row if isinstance(row, dict) else {}), "status": status}
        state.data["opencode_turns"] = ledger


def _run_opencode_payload(job_path: Path, payload: dict) -> bool:
    """Finalize one payload; return whether its durable work is complete."""
    from compchem_memory import opencode_ingest

    paths = _opencode_paths(payload)
    session_id = str(payload["session_id"])
    exported = opencode_ingest.live_session_exporter()(session_id)
    if not isinstance(exported, dict):
        return False
    messages = _finalize_opencode_capture(paths, session_id, exported)
    turn_id = _payload_turn_id(payload, exported, messages)
    operation = str(payload["operation"])
    if turn_id is None:
        return False
    owner = job_path.name
    claim = _claim_opencode_operation(paths, session_id, operation, turn_id, owner)
    if claim == "done":
        return True
    if claim == "busy":
        return False
    try:
        if operation == "critic":
            from compchem_memory.hook.critic import _has_verdict, run_critic

            run_critic(paths, "opencode", session_id, None, turn_id)
            verdict_path = paths.store / "claim-critic" / f"{safe_component(session_id)}.jsonl"
            if not _has_verdict(verdict_path, "opencode", turn_id):
                _finish_opencode_operation(paths, session_id, operation, turn_id, "failed")
                return False
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
            raise ValueError(f"unknown OpenCode operation: {operation}")
    except Exception as error:  # noqa: BLE001 - one payload must not abort the drain
        from compchem_memory.hook.errors import log_error

        _finish_opencode_operation(paths, session_id, operation, turn_id, "failed")
        log_error(error, "opencode", "background", paths.store, {}, "opencode-turn")
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
            "ts": time.time(), "payload": destination.name, "reason": reason,
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
    return payload


def drain_opencode_payloads(store: Path) -> None:
    """Drain every pending OpenCode payload for a project, isolated by session."""
    store = Path(store)
    worker_dir = store / "hook-workers"
    if not worker_dir.exists():
        return
    for candidate in sorted(worker_dir.glob("opencode-turn-*.json")):
        try:
            payload = _read_opencode_payload(candidate, store)
        except Exception as error:  # noqa: BLE001 - reject one and continue
            if candidate.exists():
                try:
                    _move_payload(candidate, f"invalid payload: {error}", "rejected")
                except Exception:
                    pass
            continue
        try:
            if _payload_age(candidate, payload) > _OPENCODE_PAYLOAD_MAX_AGE_SECONDS:
                _move_payload(candidate, "payload exceeded 86400 second retry bound", "expired")
                continue
            session_id = str(payload["session_id"])
            lock_path = _worker_lock(store, "opencode", session_id)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if _run_opencode_payload(candidate, payload):
                    candidate.unlink(missing_ok=True)
        except Exception as error:  # noqa: BLE001 - leave retryable and continue
            try:
                paths = _opencode_paths(payload)
                from compchem_memory.hook.errors import log_error

                log_error(error, "opencode", "background", paths.store, {}, "opencode-drain")
            except Exception:
                pass


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
    drain_opencode_payloads(store)


def main() -> None:
    """Run the job path supplied by the spawning hook."""
    if len(sys.argv) == 3 and sys.argv[1] == "--opencode-turn":
        run_opencode_job(Path(sys.argv[2]))
    elif len(sys.argv) == 2:
        run_job(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
