"""Session compaction: three-tier strategy for managing conversation context."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compchem_memory.llm import is_llm_available, call_llm


AUTOCOMPACT_BUFFER = 2000
WARNING_THRESHOLD = 4000
MIN_MESSAGES_TO_KEEP = 5
MIN_TOKENS_TO_KEEP = 10000

COMPACTION_SYSTEM_PROMPT = (
    "Summarize this computational chemistry session's tool calls, errors, and resolutions "
    "into structured notes. Preserve key parameters, successful strategies, and error fixes. "
    "Format as concise markdown with sections for: Tools Used, Errors & Resolutions, Key Results."
)


@dataclass
class CompactionResult:
    kept_messages: list[dict[str, Any]]
    summary: str
    pruned_count: int
    tokens_before: int
    tokens_after: int


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(json.dumps(part))
        total += estimate_tokens(json.dumps(msg.get("tool_calls", [])))
    return total


def maybe_compact_session(
    session_path: Path,
    model_context_window: int = 128000,
) -> CompactionResult | None:
    if not session_path.exists():
        return None

    events = _read_session_events(session_path)
    if not events:
        return None

    tokens = sum(estimate_tokens(json.dumps(ev)) for ev in events)
    threshold = model_context_window - AUTOCOMPACT_BUFFER

    if tokens < threshold:
        return None

    result = _try_session_memory_compact(events, threshold)
    if result is not None:
        _write_compacted_session(session_path, result)
        # Replace original with compacted version
        compacted_path = session_path.with_suffix(".compacted.jsonl")
        try:
            session_path.rename(session_path.with_suffix(".backup.jsonl"))
            compacted_path.rename(session_path)
        except OSError:
            pass
        return result

    # Heuristic didn't compact enough — try agent-based compaction
    if is_llm_available() and len(events) < MIN_MESSAGES_TO_KEEP:
        result = compact_with_agent(events, max_tokens=threshold)
        if result is not None:
            _write_compacted_session(session_path, result)
            compacted_path = session_path.with_suffix(".compacted.jsonl")
            try:
                session_path.rename(session_path.with_suffix(".backup.jsonl"))
                compacted_path.rename(session_path)
            except OSError:
                pass
            return result

    return None


def compact_with_agent(
    events: list[dict[str, Any]],
    max_tokens: int = 6000,
) -> CompactionResult | None:
    """Use LLM to compact session events into a summary."""
    events_json = json.dumps(events, default=str)
    summary = call_llm(COMPACTION_SYSTEM_PROMPT, events_json, max_tokens=max_tokens)
    if not summary:
        return None

    total_tokens = sum(estimate_tokens(json.dumps(ev)) for ev in events)
    summary_event = {
        "event_type": "compaction_summary",
        "content": summary,
        "timestamp": events[-1].get("timestamp", "") if events else "",
    }
    kept = [summary_event] + events[-MIN_MESSAGES_TO_KEEP:]
    kept_tokens = sum(estimate_tokens(json.dumps(ev)) for ev in kept)

    return CompactionResult(
        kept_messages=kept,
        summary=summary,
        pruned_count=len(events) - len(kept),
        tokens_before=total_tokens,
        tokens_after=kept_tokens,
    )


def compact_session_to_notes(
    session_path: Path,
    max_tokens: int = 6000,
) -> str | None:
    if not session_path.exists():
        return None

    events = _read_session_events(session_path)
    if not events:
        return None

    notes = _extract_compaction_notes(events)
    tokens = estimate_tokens(notes)
    if tokens > max_tokens:
        notes = notes[: max_tokens * 4]
    return notes


def _try_session_memory_compact(
    events: list[dict[str, Any]],
    threshold: int,
) -> CompactionResult | None:
    total_tokens = sum(estimate_tokens(json.dumps(ev)) for ev in events)
    summary_parts: list[str] = []

    tool_results = [
        ev for ev in events if ev.get("event_type") in ("tool_success", "tool_error")
    ]
    for tr in tool_results[:-5]:
        tool = tr.get("tool", "unknown")
        result_summary = tr.get("result_summary", tr.get("error", ""))
        if result_summary:
            summary_parts.append(f"[{tool}] {str(result_summary)[:200]}")

    summary = (
        "# Session Summary (compacted)\n\n" + "\n".join(summary_parts)
        if summary_parts
        else ""
    )
    summary_tokens = estimate_tokens(summary)

    kept: list[dict[str, Any]] = []
    budget = threshold - summary_tokens - MIN_TOKENS_TO_KEEP
    used = 0

    for ev in reversed(events):
        ev_tokens = estimate_tokens(json.dumps(ev))
        if used + ev_tokens > budget:
            break
        kept.insert(0, ev)
        used += ev_tokens

    if len(kept) < MIN_MESSAGES_TO_KEEP:
        return None

    return CompactionResult(
        kept_messages=kept,
        summary=summary,
        pruned_count=len(events) - len(kept),
        tokens_before=total_tokens,
        tokens_after=used + summary_tokens,
    )


def _extract_compaction_notes(events: list[dict[str, Any]]) -> str:
    sections: list[str] = ["# Session Compaction Notes\n"]

    tool_calls = [ev for ev in events if ev.get("event_type") == "tool_call"]
    if tool_calls:
        tool_summary: dict[str, int] = {}
        for tc in tool_calls:
            name = tc.get("tool", "unknown")
            tool_summary[name] = tool_summary.get(name, 0) + 1
        sections.append("## Tools Used")
        for name, count in sorted(tool_summary.items()):
            sections.append(f"- {name}: {count}x")

    errors = [ev for ev in events if ev.get("event_type") == "tool_error"]
    if errors:
        sections.append("\n## Errors Encountered")
        for err in errors:
            tool = err.get("tool", "unknown")
            msg = str(err.get("error", ""))[:300]
            sections.append(f"- [{tool}] {msg}")

    successes = [ev for ev in events if ev.get("event_type") == "tool_success"]
    if successes:
        sections.append("\n## Successful Operations")
        for s in successes[-10:]:
            tool = s.get("tool", "unknown")
            summary = str(s.get("result_summary", ""))[:200]
            sections.append(f"- [{tool}] {summary}")

    return "\n".join(sections)


def _read_session_events(session_path: Path) -> list[dict[str, Any]]:
    events = []
    try:
        with open(session_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return events


def _write_compacted_session(session_path: Path, result: CompactionResult) -> None:
    compacted_path = session_path.with_suffix(".compacted.jsonl")
    try:
        with open(compacted_path, "w") as f:
            for ev in result.kept_messages:
                f.write(json.dumps(ev) + "\n")
    except OSError:
        pass
