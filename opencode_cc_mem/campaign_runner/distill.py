"""Drain campaign worker/reviewer sessions into Magnolia staging memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import CampaignError
from .state import utc_now


class DistillError(CampaignError):
    """Raised when the campaign distillation queue cannot be drained."""


def _queue_records(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "control" / "distill-queue.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DistillError(f"invalid distill queue entry: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def _done_sessions(control_dir: Path) -> set[tuple[str, str]]:
    path = control_dir / "distill-done.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    done: set[tuple[str, str]] = set()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("session_id"):
            done.add((str(value.get("harness") or "opencode"), str(value["session_id"])))
    return done


def _append_done(control_dir: Path, session: Any, result: Mapping[str, Any]) -> None:
    path = control_dir / "distill-done.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": utc_now(),
        "harness": session.harness,
        "session_id": session.session_id,
        "session_key": session.key,
        "status": result.get("status", "committed"),
        "saved": [str(item) for item in result.get("saved", [])],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()


def drain_queue(
    run_dir: str | Path,
    project_dir: str | Path,
    db_path: str | Path,
    *,
    commit: bool = True,
    limit: int | None = None,
    exporter: Callable[[str], dict | None] | None = None,
    distiller: Callable[[str], list[dict[str, Any]] | None] | None = None,
) -> dict[str, Any]:
    """Distill queued sessions through the existing Magnolia pipeline."""
    try:
        from compchem_memory.opencode_ingest import distill_session_transcript
    except ImportError as exc:
        raise DistillError(
            "compchem-memory is not importable; run magnolia-campaign with the "
            "project virtual environment"
        ) from exc
    if exporter is None:
        try:
            from compchem_memory.opencode_ingest import v2_db_exporter
        except ImportError as exc:
            raise DistillError(
                "this compchem-memory version cannot export v2 sessions"
            ) from exc
        exporter = v2_db_exporter(str(db_path))
    run = Path(run_dir).resolve()
    project = Path(project_dir).resolve()
    if not project.is_dir():
        raise DistillError(f"project directory does not exist: {project}")
    records = _queue_records(run)
    done = _done_sessions(run / "control")
    from compchem_memory.transcript_source import SessionRef

    sessions: list[tuple[SessionRef, str | dict[str, str]]] = []
    for record in records:
        neutral_values = record.get("sessions", [])
        legacy_values = record.get("session_ids", [])
        if not isinstance(neutral_values, list) or not isinstance(legacy_values, list):
            raise DistillError("distill queue sessions must be arrays")
        for value in neutral_values:
            if not isinstance(value, Mapping):
                raise DistillError("distill queue session must be an object")
            harness = value.get("harness")
            session_id = value.get("session_id")
            key = value.get("key")
            if not all(isinstance(item, str) and item for item in (harness, session_id, key)):
                raise DistillError("distill queue session identity is incomplete")
            ref = SessionRef(harness, session_id, key, None, None, False)
            display = {"harness": harness, "session_id": session_id, "key": key}
            if (harness, session_id) not in done:
                sessions.append((ref, display))
        for session_id in legacy_values:
            if not isinstance(session_id, str):
                continue
            ref = SessionRef("opencode", session_id, session_id, None, None, True)
            if (ref.harness, ref.session_id) not in done:
                sessions.append((ref, session_id))
    unique: dict[tuple[str, str], tuple[SessionRef, str | dict[str, str]]] = {}
    for ref, display in sessions:
        unique.setdefault((ref.harness, ref.session_id), (ref, display))
    sessions = list(unique.values())
    if limit is not None:
        sessions = sessions[:limit]
    completed: list[Any] = []
    unavailable: list[Any] = []
    for session, display in sessions:
        result = distill_session_transcript(
            str(project / ".magnolia"),
            display if isinstance(display, str) else session,
            commit=commit,
            exporter=exporter,
            distiller=distiller,
        )
        if result is None:
            unavailable.append(display)
            continue
        if commit:
            _append_done(run / "control", session, result)
        completed.append(display)
    return {
        "queued": len(records),
        "sessions": len(sessions),
        "completed": completed,
        "unavailable": unavailable,
        "commit": commit,
    }
