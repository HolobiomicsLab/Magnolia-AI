"""Token, cost, and core-hour accounting."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Mapping

from .workers import empty_usage


def read_session_usage(db_path: str | Path, session_id: str) -> dict[str, float]:
    usage = empty_usage()
    path = Path(db_path)
    if not path.is_file():
        return usage
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return usage
    try:
        rows = connection.execute(
            "SELECT data FROM session_message WHERE session_id=? AND type='assistant'",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return usage
    finally:
        connection.close()
    for (raw,) in rows:
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            continue
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            usage["input_tokens"] += float(tokens.get("input", 0) or 0)
            usage["output_tokens"] += float(tokens.get("output", 0) or 0)
            usage["reasoning_tokens"] += float(tokens.get("reasoning", 0) or 0)
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                usage["cache_read_tokens"] += float(cache.get("read", 0) or 0)
                usage["cache_write_tokens"] += float(cache.get("write", 0) or 0)
        cost = data.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            usage["cost_usd"] += float(cost)
    return usage


def merge_usage(total: Mapping[str, float], addition: Mapping[str, float]) -> dict[str, float]:
    merged = {key: float(value) for key, value in total.items()}
    for key, value in addition.items():
        merged[key] = merged.get(key, 0.0) + float(value)
    return merged


def elapsed_to_core_hours(elapsed: str, ncores: int) -> float:
    parts = elapsed.strip().split(":")
    if len(parts) != 3:
        return 0.0
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return 0.0
    total_hours = hours + minutes / 60 + seconds / 3600
    return max(0.0, total_hours) * max(0, ncores)
