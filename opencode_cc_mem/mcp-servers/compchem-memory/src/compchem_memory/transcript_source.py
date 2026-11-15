"""Identify and export legacy OpenCode and harness-neutral transcripts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from compchem_memory.hook.sessions import iter_sessions
from compchem_memory.hook.state import safe_component
from compchem_memory.hook.transcript import read_turns, transcript_path
from compchem_memory.opencode_ingest import (
    _latest_sid,
    _read_mapping_ids,
    plugin_dump_exporter,
)

Export = dict | None
LegacyExporter = Callable[[str], Export]
SessionExporter = Callable[["SessionRef"], Export]


@dataclass(frozen=True)
class SessionRef:
    """One client session and its stable distillation/provenance identity."""

    harness: str
    session_id: str
    key: str
    first_ts: str | None
    last_ts: str | None
    sealable: bool


def list_sessions(store: str | Path) -> list[SessionRef]:
    """List legacy sessions first, then hook-fed sessions by first timestamp."""
    root = Path(store)
    mapping = root / "opencode-sessions.jsonl"
    first_by_sid, last_by_sid = _mapping_timestamps(mapping)
    legacy_ids = _read_mapping_ids(mapping)
    legacy = [
        SessionRef(
            harness="opencode",
            session_id=sid,
            key=sid,
            first_ts=first_by_sid.get(sid),
            last_ts=last_by_sid.get(sid),
            sealable=True,
        )
        for sid in legacy_ids
    ]

    legacy_set = set(legacy_ids)
    hook_fed: list[SessionRef] = []
    seen: set[tuple[str, str]] = set()
    for row in iter_sessions(root):
        harness = row.get("harness")
        session_id = row.get("session_id")
        if not isinstance(harness, str) or not harness:
            continue
        if not isinstance(session_id, str) or not session_id:
            continue
        if harness == "opencode" and session_id in legacy_set:
            continue
        identity = (harness, session_id)
        if identity in seen:
            continue
        seen.add(identity)
        first_ts = row.get("ts") if isinstance(row.get("ts"), str) else None
        path = transcript_path(root, harness, session_id)
        hook_fed.append(SessionRef(
            harness=harness,
            session_id=session_id,
            key=session_id if harness == "opencode" else f"{harness}-{safe_component(session_id)}",
            first_ts=first_ts,
            last_ts=_mtime_utc(path),
            sealable=False,
        ))

    hook_fed.sort(key=lambda ref: (ref.first_ts is None, ref.first_ts or ""))
    return legacy + hook_fed


def active_opencode_sid(store: str | Path) -> str | None:
    """Return the active legacy OpenCode id using the existing mapping rule."""
    return _latest_sid(Path(store) / "opencode-sessions.jsonl")


def exporter_for(
    store: str | Path,
    injected: LegacyExporter | None = None,
) -> SessionExporter:
    """Return an exporter preserving the legacy OpenCode string-id boundary."""
    legacy = injected or plugin_dump_exporter(str(store))
    neutral = magnolia_exporter(store)

    def export(ref: SessionRef) -> Export:
        if ref.harness == "opencode":
            return legacy(ref.session_id)
        return neutral(ref)

    return export


def magnolia_exporter(store: str | Path) -> SessionExporter:
    """Convert append-only hook rows to the OpenCode-v1 message family."""
    root = Path(store)

    def export(ref: SessionRef) -> Export:
        path = transcript_path(root, ref.harness, ref.session_id)
        if not path.exists():
            return None
        messages: list[dict] = []
        for index, row in enumerate(read_turns(path)):
            message = _row_message(ref, row, index)
            if message is not None:
                messages.append(message)
        return {"messages": messages}

    return export


def resolve_transcript(store: str | Path, key: str) -> Path | None:
    """Resolve a provenance key to its legacy or hook-fed transcript file."""
    root = Path(store)
    for path in (
        root / "opencode-transcripts" / f"{key}.json",
        root / "sessions" / f"{key}.jsonl",
    ):
        if path.exists():
            return path

    for row in iter_sessions(root):
        harness = row.get("harness")
        session_id = row.get("session_id")
        if not isinstance(harness, str) or not harness:
            continue
        if not isinstance(session_id, str) or not session_id:
            continue
        candidate = (
            session_id if harness == "opencode"
            else f"{harness}-{safe_component(session_id)}"
        )
        if candidate != key:
            continue
        path = transcript_path(root, harness, session_id)
        return path if path.exists() else None
    return None


def _mapping_timestamps(mapping: Path) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """Return the first and last row timestamps for each valid legacy id."""
    first: dict[str, str | None] = {}
    last: dict[str, str | None] = {}
    try:
        lines = mapping.read_text(encoding="utf-8").splitlines()
    except OSError:
        return first, last
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        session_id = row.get("opencode_session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        timestamp = row.get("ts") if isinstance(row.get("ts"), str) else None
        if session_id not in first:
            first[session_id] = timestamp
        last[session_id] = timestamp
    return first, last


def _mtime_utc(path: Path) -> str | None:
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        return None
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _row_message(ref: SessionRef, row: dict, index: int) -> dict | None:
    kind = row.get("kind")
    info = {
        "id": f"{ref.key}:{index}",
        "role": "user" if kind == "prompt" else "assistant",
        "agent": row.get("agent"),
    }
    if kind in {"prompt", "assistant"}:
        return {"info": info, "parts": [{"type": "text", "text": row.get("text", "")}]}
    if kind != "tool" or row.get("phase") != "post":
        return None

    name = row.get("name") or "tool"
    state = {
        "status": "error" if row.get("is_error") else "completed",
        "input": row.get("input", {}),
        "output": row.get("output"),
    }
    part = {
        "type": "tool",
        "tool": name,
        "name": name,
        "server": row.get("server"),
        "state": state,
    }
    return {"info": info, "parts": [part]}
