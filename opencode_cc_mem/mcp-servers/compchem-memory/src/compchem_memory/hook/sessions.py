"""Map neutral hook sessions and legacy OpenCode captures to one schema."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from compchem_memory.hook.state import private_mkdir, safe_component
from compchem_memory.hook.transcript import _append_jsonl, _timestamp


def record_session(
    store: Path,
    harness: str,
    session_id: str,
    cwd: str,
    transcript_ref: str | None,
    model: str | None = None,
    role: str | None = None,
) -> bool:
    """Record a session once, using an exclusive marker before its locked append."""
    directory = Path(store) / "hook-state"
    private_mkdir(directory)
    name = f"session-recorded-{safe_component(harness)}-{safe_component(session_id)}"
    try:
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(descriptor)
    origin = {"client": harness, "session_id": session_id}
    if model:
        origin["model"] = model
    bound_role = role or os.environ.get("MAGNOLIA_ROLE")
    if bound_role:
        origin["role"] = bound_role
    row = {"ts": _timestamp(), "harness": harness, "session_id": session_id,
           "directory": cwd, "transcript_ref": transcript_ref, "origin": origin}
    _append_jsonl(Path(store) / "agent-sessions.jsonl", row)
    return True


def iter_sessions(store: Path) -> Iterator[dict]:
    """Yield valid neutral rows followed by field-mapped legacy OpenCode rows."""
    for raw in _read_rows(Path(store) / "agent-sessions.jsonl"):
        yield raw
    for raw in _read_rows(Path(store) / "opencode-sessions.jsonl"):
        session_id = raw.pop("opencode_session_id", None)
        yield {**raw, "harness": "opencode", "session_id": session_id,
               "transcript_ref": raw.get("transcript_ref")}


def _read_rows(path: Path) -> Iterator[dict]:
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except FileNotFoundError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            yield row
