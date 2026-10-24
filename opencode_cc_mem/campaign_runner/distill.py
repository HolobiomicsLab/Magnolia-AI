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


def _done_sessions(control_dir: Path) -> set[str]:
    path = control_dir / "distill-done.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    done: set[str] = set()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("session_id"):
            done.add(str(value["session_id"]))
    return done


def _append_done(control_dir: Path, session_id: str, result: Mapping[str, Any]) -> None:
    path = control_dir / "distill-done.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": utc_now(),
        "session_id": session_id,
        "status": result.get("status", "committed"),
        "saved": result.get("saved", []),
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
    sessions: list[str] = []
    for record in records:
        values = record.get("session_ids", [])
        if not isinstance(values, list):
            raise DistillError("distill queue session_ids must be an array")
        for session_id in values:
            if isinstance(session_id, str) and session_id not in done:
                sessions.append(session_id)
    sessions = list(dict.fromkeys(sessions))
    if limit is not None:
        sessions = sessions[:limit]
    completed: list[str] = []
    unavailable: list[str] = []
    for session_id in sessions:
        result = distill_session_transcript(
            str(project / ".magnolia"),
            session_id,
            commit=commit,
            exporter=exporter,
            distiller=distiller,
        )
        if result is None:
            unavailable.append(session_id)
            continue
        if commit:
            _append_done(run / "control", session_id, result)
        completed.append(session_id)
    return {
        "queued": len(records),
        "sessions": len(sessions),
        "completed": completed,
        "unavailable": unavailable,
        "commit": commit,
    }
