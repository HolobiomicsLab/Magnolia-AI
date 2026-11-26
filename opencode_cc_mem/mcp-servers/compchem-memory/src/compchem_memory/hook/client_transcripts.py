"""Recover assistant reply blocks from client-owned transcript journals.

The formats are observed client internals rather than public Magnolia schemas,
so every reader is deliberately narrow and the public function fails open.
Only a bounded tail is read; malformed and unfamiliar rows are ignored.
A tail that starts mid-turn loses that turn's earlier blocks by design.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from compchem_memory.opencode_ingest import scrub_secrets


def assistant_texts(
    harness: str,
    transcript_ref: str,
    *,
    turn_id: str | None,
    agent_id: str | None,
    max_bytes: int = 2_000_000,
) -> list[str]:
    """Return scrubbed assistant blocks for one turn, oldest first.

    ``transcript_ref`` is accepted only as the regular file supplied by Claude
    or Codex, or as Vibe's session directory. Any I/O race, invalid JSON or
    changed client shape produces an empty result rather than breaking a hook.
    """
    if not isinstance(turn_id, str) or not turn_id:
        return []
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        return []
    try:
        path = Path(transcript_ref)
        if harness == "claude" and path.is_file():
            rows = _json_rows(_tail_text(path, max_bytes))
            return _scrub(_claude_blocks(rows, turn_id, agent_id))
        if harness == "codex" and path.is_file():
            return _scrub(_codex_blocks(_json_rows(_tail_text(path, max_bytes)), turn_id, agent_id))
        if harness == "vibe" and path.is_dir():
            rows = _json_rows_many(_vibe_tail_texts(path, max_bytes))
            return _scrub(_vibe_blocks(rows, turn_id, agent_id))
    except Exception:  # noqa: BLE001 - a client transcript must never break its session
        return []
    return []


def latest_vibe_assistant_turn(
    transcript_ref: str,
    *,
    agent_id: str | None,
    max_bytes: int = 2_000_000,
) -> str | None:
    """Return the latest Vibe assistant turn for one agent, without raising."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        return None
    turns = vibe_assistant_turns(
        transcript_ref, agent_id=agent_id, max_bytes=max_bytes,
    )
    return turns[-1] if turns else None


def latest_vibe_turn(
    transcript_ref: str,
    *,
    agent_id: str | None,
    max_bytes: int = 2_000_000,
) -> str | None:
    """Return the latest visible Vibe user or assistant turn for one agent."""
    turns = vibe_turns(transcript_ref, agent_id=agent_id, max_bytes=max_bytes)
    return turns[-1] if turns else None


def vibe_turns(
    transcript_ref: str,
    *,
    agent_id: str | None,
    max_bytes: int = 2_000_000,
) -> list[str]:
    """Return ordered Vibe turns visible through user or assistant entries."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        return []
    try:
        session = Path(transcript_ref)
        if not session.is_dir():
            return []
        rows = _json_rows_many(_vibe_tail_texts(session, max_bytes))
        entries = [
            entry for entry in _vibe_entries(rows)
            if entry.get("role") in {"user", "assistant"}
        ]
        has_exact_agent = any(_vibe_exact_agent(entry, agent_id) for entry in entries)
        turns = []
        for entry in entries:
            turn_id = entry.get("turnId")
            if (_vibe_matches_agent(entry, agent_id, has_exact_agent)
                    and isinstance(turn_id, str) and turn_id and turn_id not in turns):
                turns.append(turn_id)
        return turns
    except Exception:  # noqa: BLE001 - a client transcript must never break its session
        return []


def vibe_assistant_turns(
    transcript_ref: str,
    *,
    agent_id: str | None,
    max_bytes: int = 2_000_000,
) -> list[str]:
    """Return one ordered occurrence of each visible Vibe assistant turn."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        return []
    try:
        session = Path(transcript_ref)
        if not session.is_dir():
            return []
        rows = _json_rows_many(_vibe_tail_texts(session, max_bytes))
        entries = _vibe_assistant_entries(rows)
        has_exact_agent = any(_vibe_exact_agent(entry, agent_id) for entry in entries)
        turns = []
        for entry in entries:
            turn_id = entry.get("turnId")
            if (_vibe_matches_agent(entry, agent_id, has_exact_agent)
                    and isinstance(turn_id, str) and turn_id and turn_id not in turns):
                turns.append(turn_id)
        return turns
    except Exception:  # noqa: BLE001 - a client transcript must never break its session
        return []


def _tail_text(path: Path, max_bytes: int) -> str:
    """Read at most ``max_bytes`` from a file tail, excluding a partial row."""
    size = path.stat().st_size
    # One byte of a truncated read budget identifies whether the logical tail
    # starts on a line boundary; the remaining bytes reach the end of file.
    start = 0 if size <= max_bytes else size - max_bytes + 1
    read_start = start if start == 0 else start - 1
    with path.open("rb") as stream:
        stream.seek(read_start)
        data = stream.read(max_bytes)
    if start:
        preceding = data[:1]
        data = data[1:]
        if preceding != b"\n":
            newline = data.find(b"\n")
            if newline < 0:
                return ""
            data = data[newline + 1:]
    return data.decode("utf-8", errors="replace")


def _vibe_tail_texts(session: Path, max_bytes: int) -> list[str]:
    """Read journal tails in name order within one aggregate byte budget."""
    journal = session / "journal"
    if not journal.is_dir():
        return []
    files = sorted(
        path for path in journal.iterdir()
        if path.name.endswith(".jsonl") and path.is_file()
    )
    selected: list[tuple[Path, int]] = []
    remaining = max_bytes
    for path in reversed(files):
        if remaining <= 0:
            break
        allowance = min(path.stat().st_size, remaining)
        if allowance:
            selected.append((path, allowance))
            remaining -= allowance
    return [_tail_text(path, allowance) for path, allowance in reversed(selected)]


def _json_rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _json_rows_many(texts: Iterable[str]) -> list[dict]:
    rows = []
    for text in texts:
        rows.extend(_json_rows(text))
    return rows


def _claude_blocks(rows: list[dict], turn_id: str, agent_id: str | None) -> list[str]:
    """Attribute assistant rows to the closest preceding prompted user row."""
    current_turn = None
    blocks = []
    for row in rows:
        if row.get("type") == "user":
            prompt_id = row.get("promptId")
            if isinstance(prompt_id, str) and prompt_id:
                current_turn = prompt_id
            continue
        if row.get("type") != "assistant" or current_turn != turn_id:
            continue
        if not _matches_agent(row, agent_id):
            continue
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") if part.get("type") == "text" else None
            if part.get("type") == "tool_use" and part.get("name") == "SubagentHandback":
                tool_input = part.get("input")
                text = tool_input.get("message") if isinstance(tool_input, dict) else None
            if isinstance(text, str) and text:
                blocks.append(text)
    return blocks


def _codex_blocks(rows: list[dict], turn_id: str, agent_id: str | None) -> list[str]:
    """Assign assistant messages between Codex task boundaries to one turn."""
    if agent_id is not None:
        return []
    current_turn = None
    pending: list[str] = []
    blocks: list[str] = []
    for row in rows:
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")

        boundary_turn = payload.get("turn_id")
        if row_type == "event_msg" and payload_type == "task_started":
            current_turn = boundary_turn if isinstance(boundary_turn, str) else None
            pending.clear()
            continue
        if row_type == "turn_context":
            if current_turn is None and pending and boundary_turn == turn_id:
                blocks.extend(pending)
            pending.clear()
            current_turn = boundary_turn if isinstance(boundary_turn, str) else None
            continue
        is_assistant = (
            row_type == "response_item"
            and payload_type == "message"
            and payload.get("role") == "assistant"
        )
        if is_assistant:
            texts = _content_texts(payload.get("content"), "output_text")
            if current_turn == turn_id:
                blocks.extend(texts)
            elif current_turn is None:
                pending.extend(texts)
            continue
        if row_type == "event_msg" and payload_type == "task_complete":
            if current_turn is None and boundary_turn == turn_id:
                blocks.extend(pending)
            pending.clear()
            current_turn = None
    return blocks


def _vibe_blocks(rows: list[dict], turn_id: str, agent_id: str | None) -> list[str]:
    """Read assistant entries while excluding role-less reasoning entries."""
    entry_order: list[str] = []
    entries: dict[str, list[str]] = {}
    anonymous = 0
    assistant_entries = _vibe_assistant_entries(rows)
    has_exact_agent = any(_vibe_exact_agent(entry, agent_id) for entry in assistant_entries)
    for entry in assistant_entries:
        if (entry.get("turnId") != turn_id
                or not _vibe_matches_agent(entry, agent_id, has_exact_agent)):
            continue
        texts = _content_texts(entry.get("content"), "text")
        if not texts:
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            anonymous += 1
            entry_id = f"__anonymous_{anonymous}"
        if entry_id not in entries:
            entry_order.append(entry_id)
        entries[entry_id] = texts
    return [text for entry_id in entry_order for text in entries[entry_id]]


def _vibe_assistant_entries(rows: list[dict]) -> list[dict]:
    """Flatten role-tagged Vibe assistant entries in journal order."""
    return [entry for entry in _vibe_entries(rows) if entry.get("role") == "assistant"]


def _vibe_entries(rows: list[dict]) -> list[dict]:
    """Flatten Vibe journal entries in their observed order."""
    entries = []
    for row in rows:
        payload = row.get("payload")
        deltas = payload.get("delta") if isinstance(payload, dict) else None
        if not isinstance(deltas, list):
            continue
        entries.extend(
            entry for delta in deltas
            if isinstance(delta, dict)
            and isinstance((entry := delta.get("entry")), dict)
        )
    return entries


def _vibe_exact_agent(entry: dict, agent_id: str | None) -> bool:
    """Whether a Vibe entry explicitly identifies this child session."""
    return agent_id is not None and (
        entry.get("agentId") == agent_id or entry.get("sessionId") == agent_id
    )


def _vibe_matches_agent(entry: dict, agent_id: str | None, has_exact_agent: bool) -> bool:
    """Match shared journals, or untagged entries in a child-specific journal."""
    if agent_id is None:
        return _matches_agent(entry, None)
    if has_exact_agent:
        return _vibe_exact_agent(entry, agent_id)
    return (
        entry.get("agentId") is None
        and entry.get("sessionId") in (None, agent_id)
        and entry.get("isSidechain") is not True
    )


def _matches_agent(row: dict, agent_id: str | None) -> bool:
    row_agent = row.get("agentId")
    if agent_id is not None:
        return row_agent == agent_id
    return row_agent is None and row.get("isSidechain") is not True


def _content_texts(content: object, part_type: str) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        part["text"] for part in content
        if isinstance(part, dict)
        and part.get("type") == part_type
        and isinstance(part.get("text"), str)
        and part["text"]
    ]


def _scrub(blocks: Iterable[str]) -> list[str]:
    return [scrub_secrets(block) for block in blocks]
