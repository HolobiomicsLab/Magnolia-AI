"""Audit an agent report against its scrubbed, harness-neutral tool trace."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState, private_mkdir, safe_component
from compchem_memory.hook.transcript import read_turns, transcript_path
from compchem_memory.llm import CallResult, Route, call_route, resolve_route


MAX_OUTPUT_CHARS = 2000
MAX_OUTPUT_CHARS_OLD = 500
MAX_TRACE_CHARS = 80_000
MIN_REPORT_CHARS = 200
EVIDENCE_TOOLS = {
    "memory_get_context", "memory_get_run_history", "memory_search", "memory_scan_headers",
}
SYSTEM = (
    "You are a verification auditor for a scientific computing assistant. You are given "
    "(1) the assistant's REPORT (from the CURRENT turn) and (2) the TOOL_TRACE of "
    "ALL tools the assistant ran across the ENTIRE session (tools called, their inputs, "
    "and their real outputs; older tools may have shorter output summaries). "
    "Claims in the report may be supported by tools run in earlier turns, not just "
    "the current one. Flag any claim in the report that is NOT supported by any action "
    "in the trace — e.g. a computed number, energy, ranking, or interaction mechanism "
    "that no tool output actually produced, or analysis steps the report implies but "
    "the trace shows were skipped. Judge ONLY whether the report's claims are backed "
    "by the actions/outputs in the trace, not abstract scientific correctness. "
    'Respond with strict JSON: {"flag": boolean, "severity": "none|low|high", '
    '"unsupported_claims": [string], "why": string}.'
)


def _matches_agent(row: dict, agent_id: str | None) -> bool:
    agent = row.get("agent")
    if agent_id is None:
        return agent is None
    return isinstance(agent, dict) and agent.get("id") == agent_id


def _current_boundary(rows: list[tuple[int, dict]], agent_id: str | None) -> int:
    boundary_kind = "prompt" if agent_id is None else "agent-start"
    return max((index for index, row in rows if row.get("kind") == boundary_kind), default=-1)


def _report(rows: list[tuple[int, dict]], boundary: int) -> str:
    parts = [
        row.get("text", "") for index, row in rows
        if index > boundary and row.get("kind") == "assistant" and row.get("text")
    ]
    return "\n".join(parts).strip()


def _trace_item(row: dict, is_current: bool, status: str) -> dict:
    name = row.get("name")
    raw_output = row.get("output", "") if status != "unfinished" else ""
    output = str(raw_output) if raw_output is not None else ""
    if name not in EVIDENCE_TOOLS:
        output = output[:MAX_OUTPUT_CHARS if is_current else MAX_OUTPUT_CHARS_OLD]
    item = {
        "tool": name, "call_id": row.get("call_id"), "status": status,
        "input": row.get("input"), "output": output,
    }
    if not is_current:
        item["turn"] = "earlier"
    return item


def _tool_items(rows: list[tuple[int, dict]], boundary: int) -> list[dict]:
    items: list[dict] = []
    pending: dict[tuple[object, object], list[int]] = {}
    for index, row in rows:
        if row.get("kind") != "tool" or row.get("phase") not in {"pre", "post"}:
            continue
        key = (row.get("call_id"), row.get("name"))
        if row["phase"] == "pre":
            pending.setdefault(key, []).append(len(items))
            items.append(_trace_item(row, index > boundary, "unfinished"))
            continue
        status = "error" if row.get("is_error") else "completed"
        item = _trace_item(row, index > boundary, status)
        positions = pending.get(key) or []
        if positions:
            items[positions.pop(0)] = item
        else:
            items.append(item)
    return items


def _trace(rows: list[dict], agent_id: str | None) -> tuple[str, str, int]:
    selected = [(index, row) for index, row in enumerate(rows) if _matches_agent(row, agent_id)]
    boundary = _current_boundary(selected, agent_id)
    report = _report(selected, boundary)
    items = _tool_items(selected, boundary)
    trace_text = json.dumps(items, ensure_ascii=False, indent=1)
    if len(trace_text) > MAX_TRACE_CHARS:
        trace_text = trace_text[:MAX_TRACE_CHARS] + "\n…(truncated)"
    return report, trace_text, len(items)


def build_trace(rows: list[dict], agent_id: str | None) -> tuple[str, str]:
    """Return the current report and one terminal item per tool invocation."""
    report, trace_text, _ = _trace(rows, agent_id)
    return report, trace_text


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict) -> None:
    private_mkdir(path.parent)
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        output.write(line)


def _parse_verdict(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("judge reply has no JSON object")
    verdict = json.loads(text[start:end + 1])
    if not isinstance(verdict, dict):
        raise ValueError("judge reply JSON is not an object")
    return verdict


def _error_row(
    harness: str,
    session_id: str,
    agent_id: str | None,
    route: Route,
    result: CallResult,
) -> dict:
    return {
        "ts": _timestamp(), "sessionID": session_id, "harness": harness, "agent": agent_id,
        "provider": route.provider, "model": route.model, "status": result.status,
        "error": (result.error or "empty judge reply")[:300],
    }


def _verdict_row(
    harness: str,
    session_id: str,
    agent_id: str | None,
    route: Route,
    verdict: dict,
    report: str,
    n_tools: int,
) -> dict:
    severity = verdict.get("severity") if verdict.get("severity") is not None else "none"
    claims = verdict.get("unsupported_claims")
    why = verdict.get("why") if verdict.get("why") is not None else ""
    return {
        "ts": _timestamp(), "sessionID": session_id, "harness": harness, "agent": agent_id,
        "provider": route.provider, "model": route.model, "flag": bool(verdict.get("flag")),
        "severity": severity, "unsupported_claims": claims if claims is not None else [],
        "why": why, "n_tools": n_tools,
        "report_preview": report[:200],
    }


def _save_notice(paths: ProjectPaths, harness: str, session_id: str, row: dict) -> None:
    if not row["flag"]:
        return
    notice = f"⚠ ({row['severity']}) {str(row['why'])[:140]}"
    with SessionState.open(paths.store, harness, session_id) as state:
        notices = state.data.get("unshown_notices")
        if not isinstance(notices, list):
            notices = []
        notices.append(notice)
        state.data["unshown_notices"] = notices


def _judge(route: Route, report: str, trace_text: str) -> CallResult:
    user = f"TOOL_TRACE:\n{trace_text}\n\nREPORT:\n{report}\n\nReturn only the JSON."
    return call_route(
        route, SYSTEM, user, max_tokens=500, temperature=0,
        json_mode=True, disable_thinking=True,
    )


def run_critic(
    paths: ProjectPaths,
    harness: str,
    session_id: str,
    agent_id: str | None,
) -> None:
    """Run the enabled critic and persist its verdict without raising."""
    try:
        if not os.environ.get("MAGNOLIA_CRITIC"):
            return
        path = transcript_path(paths.store, harness, session_id)
        if not path.exists():
            return
        report, trace_text, n_tools = _trace(read_turns(path), agent_id)
        if len(report) < MIN_REPORT_CHARS or (route := resolve_route("critic")) is None:
            return
        result = _judge(route, report, trace_text)
        out_dir = paths.store / "claim-critic"
        if result.error or not result.text:
            _append_jsonl(out_dir / "errors.jsonl", _error_row(harness, session_id, agent_id, route, result))
            return
        try:
            verdict = _parse_verdict(result.text)
        except Exception as error:  # noqa: BLE001 - malformed judge output is logged
            malformed = CallResult(None, result.finish_reason, result.resolved_model, str(error), result.status)
            _append_jsonl(out_dir / "errors.jsonl", _error_row(harness, session_id, agent_id, route, malformed))
            return
        row = _verdict_row(harness, session_id, agent_id, route, verdict, report, n_tools)
        _append_jsonl(out_dir / f"{safe_component(session_id)}.jsonl", row)
        _save_notice(paths, harness, session_id, row)
    except Exception:  # noqa: BLE001 - a hook failure must never break the client
        return
