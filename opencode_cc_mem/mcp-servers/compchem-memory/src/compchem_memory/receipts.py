"""Derived decision receipts — a pure, deterministic view over session JSONL.

A receipt is the machine-grounded "invoice" for one allowlisted tool call
(`submit_job` first): the effective args, the delta-vs-default decision
predicate, and the outcome keyed by run_id — everything read from the
recorded session events, nothing invented. Derived on demand, never persisted
(verdict: a derived view, not a second write path).

Decision predicate (2026-09-07 verdict §3): an arg differs from the EFFECTIVE
DEFAULT. Args without a default (command, working_dir) are task inputs, not
decisions. The eight resource args collapse into ONE `resource_selection`
decision. Recipe/template pinning CANNOT be computed by either system, so
every receipt is tagged `pinning: unassessed`. `rationale` is nullable and
never LLM-backfilled (`rationale_source: none|observed|self_reported`).

Fidelity: v2 records (capture_version 2, A1) carry full structured args and
results. v1/unstamped records truncate silently at capture — the extractor
degrades gracefully: best-effort parse, `args_complete: false`, and every
caveat listed in `fidelity_notes`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from compchem_memory.capture_manifest import (
    FULL_FIDELITY_TOOLS,
    RESOURCE_ARG_KEYS,
    TOOL_DEFAULTS,
)

_RUN_ID_RE = re.compile(r'"run_id":\s*"([^"]+)"')
_JOB_ID_RE = re.compile(r'"job_id":\s*"([^"]+)"')
_SUCCESS_RE = re.compile(r'"success":\s*(true|false)')


def extract_receipts_from_events(
    events: list[dict[str, Any]],
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Pure core: events -> receipts. Pairing is per-tool FIFO — the first
    tool_success/tool_error after a call pairs with the oldest unpaired call
    of the same tool (calls can be logged back-to-back before results)."""
    receipts: list[dict[str, Any]] = []
    pending: dict[str, list[dict[str, Any]]] = {}
    call_index = 0

    for ev in events:
        tool = ev.get("tool") or ""
        etype = ev.get("event_type") or ""
        if tool not in FULL_FIDELITY_TOOLS:
            continue
        if etype == "tool_call":
            call_index += 1
            pending.setdefault(tool, []).append({
                "call": ev,
                "seq": call_index,
            })
        elif etype in ("tool_success", "tool_error"):
            queue = pending.get(tool)
            if not queue:
                continue  # result without a recorded call — not a decision of ours
            item = queue.pop(0)
            receipts.append(_build_receipt(item, ev, etype, session_id))
    # Calls that never returned a recorded result are still decisions.
    for tool, queue in pending.items():
        for item in queue:
            receipts.append(_build_receipt(item, None, None, session_id))
    return receipts


def extract_receipts(session_path: str | Path) -> list[dict[str, Any]]:
    """Read one session JSONL file and derive its receipts."""
    path = Path(session_path)
    events = [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    session_id = None
    for ev in events:
        if ev.get("session_id"):
            session_id = ev["session_id"]
            break
    return extract_receipts_from_events(events, session_id=session_id)


def _build_receipt(
    item: dict[str, Any],
    result_ev: dict[str, Any] | None,
    result_type: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    call = item["call"]
    tool = call.get("tool") or ""
    version = call.get("capture_version")
    fidelity = "full" if version == 2 else "summary"
    notes: list[str] = []
    if version != 2:
        notes.append(
            "recorded before capture_version stamps (v1): args/result "
            "truncated silently at capture"
        )

    # --- args ---
    args: dict[str, Any] | None
    args_complete: bool
    if version == 2 and isinstance(call.get("args"), dict):
        args = call["args"]
        args_complete = not call.get("args_truncated", False)
        if call.get("args_truncated"):
            notes.append("args hit the v2 safety cap; structured args omitted")
    else:
        args = _parse_v1_args_summary(call.get("args_summary", ""))
        args_complete = False
        if args:
            notes.append("args parsed from lossy v1 args_summary; delta may under-report")
    if args and "project_dir" in args:
        args = {k: v for k, v in args.items() if k != "project_dir"}

    # --- outcome ---
    outcome: dict[str, Any] | None = None
    if result_ev is None:
        notes.append("no tool_success/tool_error recorded for this call")
    elif result_type == "tool_error":
        outcome = {"success": False, "error": result_ev.get("error", "")}
        if result_ev.get("error_truncated"):
            notes.append("error message truncated at capture")
    elif result_ev.get("capture_version") == 2 and isinstance(
        result_ev.get("result"), dict
    ):
        r = result_ev["result"]
        outcome = {
            "success": r.get("success"),
            "run_id": r.get("run_id"),
            "job_id": r.get("job_id"),
        }
    else:
        outcome = _parse_v1_result_summary(result_ev.get("result_summary", ""))
        if outcome and outcome.get("_regex"):
            notes.append(
                "outcome recovered from truncated v1 result via pattern match"
            )
        elif outcome is None:
            notes.append("outcome not recoverable from v1 result (severed at capture)")

    sid = session_id or call.get("session_id") or "unknown"
    return {
        "receipt_id": f"{sid}:{item['seq']}",
        "session_id": sid,
        "project_id": call.get("project_id"),
        "timestamp": call.get("timestamp"),
        "source": call.get("source"),
        "tool": tool,
        "fidelity": fidelity,
        "fidelity_notes": notes,
        "args": args,
        "args_complete": args_complete,
        "decisions": _decisions(tool, args),
        "pinning": "unassessed",
        "rationale": None,
        "rationale_source": "none",
        "outcome": outcome,
    }


def _decisions(tool: str, args: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Delta-vs-effective-default predicate; resource args collapse to one."""
    defaults = TOOL_DEFAULTS.get(tool, {})
    decisions: list[dict[str, Any]] = []
    resource_chosen: dict[str, Any] = {}
    resource_default: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k not in defaults:
            continue  # no default -> task input, not a decision
        if defaults[k] == v:
            continue
        if k in RESOURCE_ARG_KEYS:
            resource_chosen[k] = v
            resource_default[k] = defaults[k]
        else:
            decisions.append(
                {"parameter": k, "chosen": v, "default": defaults[k]}
            )
    if resource_chosen:
        decisions.append(
            {
                "parameter": "resource_selection",
                "chosen": resource_chosen,
                "default": resource_default,
            }
        )
    return decisions


def _parse_v1_args_summary(summary: str) -> dict[str, str]:
    """Best-effort parse of the legacy `k=v, k=v` summary. Unreliable by
    nature (values may contain ', ' or be cut at 80 chars) — the receipt's
    fidelity_notes say so."""
    out: dict[str, str] = {}
    for part in summary.split(", "):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = v
    return out


def _parse_v1_result_summary(summary: str) -> dict[str, Any] | None:
    """v1 results are cut at 200 chars, usually mid-JSON. Try a full parse
    first (short results survive), then recover whatever fields the prefix
    still contains by pattern — flagged via `_regex` for fidelity_notes.
    Returns None only when nothing is recoverable."""
    try:
        parsed = json.loads(summary)
        if isinstance(parsed, dict):
            return {
                "success": parsed.get("success"),
                "run_id": parsed.get("run_id"),
                "job_id": parsed.get("job_id"),
            }
    except (ValueError, TypeError):
        pass
    run_id = _RUN_ID_RE.search(summary)
    job_id = _JOB_ID_RE.search(summary)
    success = _SUCCESS_RE.search(summary)
    if not (run_id or job_id or success):
        return None
    return {
        "success": (success.group(1) == "true") if success else None,
        "run_id": run_id.group(1) if run_id else None,
        "job_id": job_id.group(1) if job_id else None,
        "_regex": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compchem-memory receipts",
        description="Derive decision receipts from a session JSONL file "
        "(derived view; nothing is persisted).",
    )
    parser.add_argument("session_jsonl", help="Path to a session .jsonl file")
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)
    receipts = extract_receipts(args.session_jsonl)
    print(json.dumps(receipts, indent=args.indent, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
