"""Persist harness-neutral events as scrubbed, append-only JSONL rows.

Deliberate change: tool inputs are capped at 4000 characters; the legacy
OpenCode trace did not cap inputs. Evidence-tool outputs remain uncapped.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.hook.events import HookEvent, ToolCall
from compchem_memory.opencode_ingest import scrub_secrets

_INPUT_LIMIT = 4000
_OUTPUT_LIMIT = 2000
_EVIDENCE_TOOLS = {
    "memory_get_context", "memory_get_run_history", "memory_search", "memory_scan_headers",
}


def transcript_path(store: Path, harness: str, session_id: str) -> Path:
    """Return the per-harness JSONL transcript path for a client session."""
    return Path(store) / "transcripts" / harness / f"{session_id}.jsonl"


def append_event(store: Path, event: HookEvent) -> None:
    """Append one scrubbed event row without interleaving concurrent writers."""
    row = _event_row(event)
    if row is None:
        return
    path = transcript_path(store, event.harness, event.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_jsonl(path, row)


def read_turns(path: Path) -> list[dict]:
    """Read transcript objects in order, skipping malformed JSONL lines."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _event_row(event: HookEvent) -> dict | None:
    agent = None if event.agent is None else {"id": event.agent.id, "type": event.agent.type}
    row = {"schema_version": event.schema_version, "ts": _timestamp(),
           "kind": event.event, "turn_id": event.turn_id, "agent": agent}
    if event.event in {"pre-tool", "post-tool"}:
        row.update(_tool_row(event.tool, phase="pre" if event.event == "pre-tool" else "post"))
    elif event.event == "prompt":
        row["text"] = _clean_text(event.prompt or "")
    elif event.event == "turn-end":
        if not event.assistant_text:
            return None
        row.update(kind="assistant", text=_clean_text(event.assistant_text))
    return row


def _tool_row(tool: ToolCall, phase: str) -> dict:
    scrubbed_input = scrub_secrets(json.dumps(tool.input, ensure_ascii=False, default=str))
    row = {"kind": "tool", "phase": phase, "name": tool.name,
           "call_id": tool.call_id, "input": _clean_input(tool.input)}
    if len(scrubbed_input) > _INPUT_LIMIT:
        row["truncated"] = True
    if phase == "post":
        limit = None if tool.name in _EVIDENCE_TOOLS else _OUTPUT_LIMIT
        row.update(output=_clean_text(tool.output or "", limit), is_error=tool.is_error)
    return row


def _clean_input(value: dict) -> dict | list | str | int | float | bool | None:
    scrubbed = scrub_secrets(json.dumps(value, ensure_ascii=False, default=str))
    if len(scrubbed) > _INPUT_LIMIT:
        return scrubbed[:_INPUT_LIMIT]
    try:
        return json.loads(scrubbed)
    except ValueError:
        return scrubbed


def _clean_text(text: str, limit: int | None = None) -> str:
    scrubbed = scrub_secrets(text)
    if limit is not None:
        scrubbed = scrubbed[:limit]
    return scrubbed.replace("\x00", "")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, row: dict) -> None:
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, line)
    finally:
        os.close(descriptor)
