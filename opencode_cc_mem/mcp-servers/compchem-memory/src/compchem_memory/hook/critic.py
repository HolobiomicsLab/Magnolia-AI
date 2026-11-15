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
MAX_BOOT_INJECTED_CHARS = 2000
MAX_INJECTED_CHARS = 1500
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


def _child_label(row: dict) -> str | None:
    agent = row.get("agent")
    if not isinstance(agent, dict) or not agent.get("id"):
        return None
    return f"[child {agent['id']}]"


def _trace_item(row: dict, is_current: bool, status: str, label_child: bool = False) -> dict:
    name = row.get("name")
    raw_output = row.get("output", "") if status != "unfinished" else ""
    output = str(raw_output) if raw_output is not None else ""
    if name not in EVIDENCE_TOOLS:
        output = output[:MAX_OUTPUT_CHARS if is_current else MAX_OUTPUT_CHARS_OLD]
    item = {
        "tool": name, "call_id": row.get("call_id"), "status": status,
        "input": row.get("input"), "output": output,
    }
    if label_child and _child_label(row):
        item["agent"] = _child_label(row)
    if not is_current:
        item["turn"] = "earlier"
    return item


def _cap_injected_output(row: dict) -> str:
    text = str(row.get("text", ""))
    if row.get("channel") == "boot":
        marker = "... [boot context truncated]"
        limit = MAX_BOOT_INJECTED_CHARS
    else:
        marker = "... [injected context truncated]"
        limit = MAX_INJECTED_CHARS
    if len(text) <= limit:
        return text
    return text[:limit - len(marker)] + marker


def _is_current_row(
    index: int,
    row: dict,
    boundary: int,
    turn_id: str | None,
    current_window: tuple[int, int] | None,
    current_indexes: set[int] | None = None,
) -> bool:
    if current_indexes is not None and index in current_indexes:
        return True
    if current_window is not None and current_window[0] <= index <= current_window[1]:
        return True
    return row.get("turn_id") == turn_id if turn_id is not None else index > boundary


def _tool_items(
    rows: list[tuple[int, dict]],
    boundary: int,
    turn_id: str | None = None,
    current_window: tuple[int, int] | None = None,
    label_children: bool = False,
    current_indexes: set[int] | None = None,
) -> list[dict]:
    items: list[dict] = []
    pending: dict[tuple[object, object], list[int]] = {}
    for index, row in rows:
        if row.get("kind") == "injected":
            item = {
                "tool": "MAGNOLIA NOTE", "call_id": row.get("tool_call_id"),
                "status": "injected", "channel": row.get("channel"),
                "entry_ids": row.get("entry_ids", []), "output": _cap_injected_output(row),
            }
            if label_children and _child_label(row):
                item["agent"] = _child_label(row)
            items.append(item)
            continue
        if row.get("kind") == "assistant" and label_children and _child_label(row):
            is_current = _is_current_row(
                index, row, boundary, turn_id, current_window, current_indexes,
            )
            output = str(row.get("text") or "")
            item = {
                "tool": "SUB-AGENT REPORT", "call_id": None, "status": "completed",
                "input": None,
                "output": output[:MAX_OUTPUT_CHARS if is_current else MAX_OUTPUT_CHARS_OLD],
                "agent": _child_label(row),
            }
            if not is_current:
                item["turn"] = "earlier"
            items.append(item)
            continue
        if row.get("kind") != "tool" or row.get("phase") not in {"pre", "post", "denied"}:
            continue
        key = (row.get("call_id"), row.get("name"))
        is_current = _is_current_row(
            index, row, boundary, turn_id, current_window, current_indexes,
        )
        if row["phase"] == "denied":
            item = _trace_item(row, is_current, "denied", label_children)
            item["tool"] = f"DENIED: {row.get('name')}"
            items.append(item)
            continue
        if row["phase"] == "pre":
            pending.setdefault(key, []).append(len(items))
            items.append(_trace_item(row, is_current, "unfinished", label_children))
            continue
        status = "error" if row.get("is_error") else "completed"
        item = _trace_item(row, is_current, status, label_children)
        positions = pending.get(key) or []
        if positions:
            items[positions.pop(0)] = item
        else:
            items.append(item)
    return items


def _parent_window(
    rows: list[dict], report_rows: list[tuple[int, dict]], turn_id: str | None,
) -> tuple[int, int] | None:
    prompts = [
        index for index, row in report_rows
        if row.get("kind") == "prompt" and (turn_id is None or row.get("turn_id") == turn_id)
    ]
    if not prompts:
        return None
    start = max(prompts)
    for index in range(start + 1, len(rows)):
        row = rows[index]
        if row.get("agent") is not None:
            continue
        if row.get("kind") == "session-end":
            return start, index
        if row.get("kind") == "prompt":
            return start, index - 1
        if row.get("kind") == "assistant" and (
            turn_id is None or row.get("turn_id") == turn_id
        ):
            return start, index
    return start, len(rows) - 1


def _agent_window(
    primary: list[tuple[int, dict]], report_rows: list[tuple[int, dict]], turn_id: str | None,
) -> tuple[int, int] | None:
    """Bound a child's current Vibe turn when its tool rows have no turn id."""
    if turn_id is None or not report_rows:
        return None
    markers = [
        index for index, row in primary
        if row.get("kind") == "turn-boundary" and row.get("turn_id") == turn_id
    ]
    if markers:
        end = markers[-1]
        previous = [
            index for index, row in primary
            if index < end and row.get("kind") == "turn-boundary"
        ]
        return (max(previous) + 1 if previous else 0, end)
    first = min(index for index, _ in report_rows)
    end = max(index for index, _ in report_rows)
    previous = [
        index for index, row in primary
        if index < first and row.get("kind") in {"assistant", "agent-start"}
    ]
    return (max(previous) + 1 if previous else 0, end)


def _fit_trace(items: list[dict]) -> str:
    """Drop oldest evidence until the trace is valid JSON within its budget."""
    # In an indent=1 JSON list each item's block does not depend on its neighbours, so the
    # serialised length is "[\n" + blocks joined by ",\n" + "\n]": measure each item once.
    sizes = [len(json.dumps([item], ensure_ascii=False, indent=1)) - 4 for item in items]
    total = sum(sizes) + 2 * max(len(sizes) - 1, 0) + (4 if sizes else 2)
    start = 0
    while total > MAX_TRACE_CHARS and len(sizes) - start > 1:
        total -= sizes[start] + 2
        start += 1
    kept = list(items[start:])
    trace_text = json.dumps(kept, ensure_ascii=False, indent=1)
    if len(trace_text) > MAX_TRACE_CHARS and kept:
        original = str(kept[0].get("output", ""))
        marker = "... [trace item truncated]\n"
        low, high = 0, len(original)
        best = {**kept[0], "output": marker}
        while low <= high:
            middle = (low + high) // 2
            suffix = original[-middle:] if middle else ""
            candidate = {**kept[0], "output": marker + suffix}
            candidate_text = json.dumps([candidate], ensure_ascii=False, indent=1)
            if len(candidate_text) <= MAX_TRACE_CHARS:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        kept[0] = best
        trace_text = json.dumps(kept, ensure_ascii=False, indent=1)
    return trace_text


def _has_matching_child_boundary(
    rows: list[dict], reply: dict, current_window: tuple[int, int],
) -> bool:
    """Return whether the reply's child turn began inside the parent window."""
    agent = reply.get("agent")
    agent_id = agent.get("id") if isinstance(agent, dict) else None
    turn_id = reply.get("turn_id")
    if not agent_id or not turn_id:
        return False
    start, end = current_window
    return any(
        start < index <= end
        and row.get("kind") == "turn-boundary"
        and row.get("turn_id") == turn_id
        and isinstance(row.get("agent"), dict)
        and row["agent"].get("id") == agent_id
        for index, row in enumerate(rows)
    )


def _late_child_reply_indexes(
    rows: list[dict],
    trace_rows: list[tuple[int, dict]],
    child_ids: set[str],
    current_window: tuple[int, int] | None,
    harness: str | None,
) -> tuple[set[int], set[int]]:
    """Return all late Vibe replies and those proven current by a boundary."""
    if harness != "vibe" or current_window is None:
        return set(), set()
    # A reply recovered after this point stays earlier-turn evidence on retry.
    next_prompt = next((
        index for index, row in enumerate(rows)
        if index > current_window[1]
        and row.get("agent") is None
        and row.get("kind") == "prompt"
    ), len(rows))
    late = {
        index for index, row in trace_rows
        if current_window[1] < index < next_prompt
        and row.get("kind") == "assistant"
        and isinstance(row.get("agent"), dict)
        and row["agent"].get("id") in child_ids
    }
    current = {
        index for index, row in trace_rows
        if index in late and _has_matching_child_boundary(rows, row, current_window)
    }
    return late, current


def _trace(
    rows: list[dict],
    agent_id: str | None,
    turn_id: str | None = None,
    *,
    harness: str | None = None,
) -> tuple[str, str, int]:
    primary = [(index, row) for index, row in enumerate(rows) if _matches_agent(row, agent_id)]
    has_turns = any(row.get("turn_id") is not None for _, row in primary)
    report_rows = (
        [(index, row) for index, row in primary if row.get("turn_id") == turn_id]
        if turn_id is not None and has_turns
        else primary
    )
    boundary = _current_boundary(report_rows, agent_id)
    report = _report(report_rows, boundary)
    current_turn = turn_id if turn_id is not None and has_turns else None
    trace_rows = primary
    current_window = (
        _parent_window(rows, report_rows, current_turn)
        if agent_id is None else _agent_window(primary, report_rows, current_turn)
    )
    if agent_id is None:
        child_ids = {
            row["agent"]["id"] for index, row in enumerate(rows)
            if isinstance(row.get("agent"), dict)
            and current_window is not None
            and current_window[0] < index <= current_window[1]
        }
        trace_rows = [
            (index, row) for index, row in enumerate(rows)
            if _matches_agent(row, None)
            or (isinstance(row.get("agent"), dict) and row["agent"].get("id") in child_ids)
        ]
        late_child_replies, current_late_child_replies = _late_child_reply_indexes(
            rows, trace_rows, child_ids, current_window, harness,
        )
    else:
        late_child_replies = set()
        current_late_child_replies = set()
    if current_turn is not None and report_rows:
        first_turn_index = min(index for index, _ in report_rows)
        last_turn_index = max(index for index, _ in report_rows)
        if current_window is not None:
            first_turn_index, last_turn_index = current_window
        # Rows of another turn recorded after this turn began belong to a
        # later turn (a late, asynchronous Stop): they are not evidence here.
        trace_rows = [
            (index, row) for index, row in trace_rows
            if (index <= last_turn_index or index in late_child_replies)
            and not (
                index > first_turn_index
                and row.get("agent") is None
                and row.get("turn_id") is not None
                and row.get("turn_id") != current_turn
            )
        ]
    items = _tool_items(
        trace_rows, boundary, current_turn, current_window, label_children=agent_id is None,
        current_indexes=current_late_child_replies,
    )
    trace_text = _fit_trace(items)
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
        "finish_reason": result.finish_reason,
        "reply_chars": len(result.text or ""),
        "upstream_provider": result.upstream_provider,
        "usage": result.usage,
        "quality_floor_dropped": result.quality_floor_dropped,
    }


def _verdict_row(
    harness: str,
    session_id: str,
    agent_id: str | None,
    route: Route,
    verdict: dict,
    report: str,
    n_tools: int,
    upstream_provider: str | None,
    usage: dict | None,
    quality_floor_dropped: bool,
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
        "upstream_provider": upstream_provider,
        "usage": usage,
        "quality_floor_dropped": quality_floor_dropped,
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
    turn_id: str | None = None,
) -> None:
    """Run the enabled critic and persist its verdict without raising."""
    try:
        if not os.environ.get("MAGNOLIA_CRITIC"):
            return
        path = transcript_path(paths.store, harness, session_id)
        if not path.exists():
            return
        report, trace_text, n_tools = _trace(
            read_turns(path), agent_id, turn_id, harness=harness,
        )
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
            malformed = CallResult(
                result.text, result.finish_reason, result.resolved_model, str(error), result.status,
                result.upstream_provider, result.usage, result.quality_floor_dropped,
            )
            _append_jsonl(out_dir / "errors.jsonl", _error_row(harness, session_id, agent_id, route, malformed))
            return
        row = _verdict_row(
            harness, session_id, agent_id, route, verdict, report, n_tools,
            result.upstream_provider,
            result.usage,
            result.quality_floor_dropped,
        )
        _append_jsonl(out_dir / f"{safe_component(session_id)}.jsonl", row)
        _save_notice(paths, harness, session_id, row)
    except Exception:  # noqa: BLE001 - a hook failure must never break the client
        return
