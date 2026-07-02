"""Audit: compute per-session compliance signals, write audit-report.md."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def run_audit(project_dir: str, lookback_n_sessions: int = 5) -> str:
    """Read the most recent N session logs; compute compliance metrics per session;
    write audit-report.md (newest first).

    Returns the path of the report.
    """
    pd = Path(project_dir)
    sessions_dir = pd / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_files = sorted(sessions_dir.glob("*.jsonl"), reverse=True)[:lookback_n_sessions]
    blocks: list[str] = []
    for sf in session_files:
        try:
            events = _read_events(sf)
        except Exception:
            continue
        if not events:
            continue
        block = _format_block(sf, events)
        if block:
            blocks.append(block)

    report_path = pd / ".magnolia" / "audit-report.md"
    new_content = "# Audit Report\n\n" + "\n\n---\n\n".join(blocks) + "\n"
    report_path.write_text(new_content)
    return str(report_path)


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _format_block(session_file: Path, events: list[dict[str, Any]]) -> str:
    header = next((e for e in events if e.get("event_type") == "session_start"), None)
    if header is None:
        return f"## {session_file.name}\n\n(legacy log; no session_start header; metrics skipped)"

    session_id = header.get("session_id", session_file.stem)

    tool_calls = [e for e in events if e.get("event_type") == "tool_call"][:3]
    called_early = any(e.get("tool") == "memory_get_context" for e in tool_calls)

    known_sources = {"compchem-tools", "compchem-memory", "magnolia-run"}
    bash_bypass = sum(
        1
        for e in events
        if e.get("event_type") == "tool_call" and e.get("source") not in known_sources
    )

    unresolved = 0
    for i, e in enumerate(events):
        if e.get("event_type") != "tool_error":
            continue
        tool = e.get("tool")
        resolved = any(
            later.get("event_type") == "tool_success" and later.get("tool") == tool
            for later in events[i + 1 :]
        )
        if not resolved:
            unresolved += 1

    return (
        f"## {session_id}\n\n"
        f"- memory_get_context_called_early: {str(called_early).lower()}\n"
        f"- bash_bypass_count: {bash_bypass}\n"
        f"- unresolved_errors: {unresolved}\n"
        f"- total_events: {len(events)}\n"
        f"- audited_at: {datetime.now(timezone.utc).isoformat()}"
    )
