# src/compchem_memory/promotion.py
"""Project→skill promotion (self-reflex rule elevation). Proposal-only +
human-confirm. Eligibility is deterministic (session count); verification is an
LLM consensus panel + consistency check; the merged/drafted rule is human-confirmed.

Detect with intelligence, gate with determinism, human-confirm — same contract as
consolidation, one tier up (project entries → skill rules)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from compchem_memory.reflex_common import (
    parse_frontmatter_file, pending_indices, prior_rejected_keys, fenced_preview,
)

_PROMOTION_MIN_SESSIONS = 3      # eligibility gate: distinct sessions
_PROMOTION_PANEL_PASSES = 3      # K independent consensus passes
_PROMOTION_PANEL_TEMPERATURE = 0.4
_PROMOTION_PANEL_APPROVE = 2     # >= this many approvals to survive


def _distinct_sessions(meta: dict[str, Any]) -> int:
    s = set(meta.get("observed_in_sessions") or [])
    sid = meta.get("opencode_session_id")
    if sid:
        s.add(sid)
    return len(s)


def eligible_entries(store_dir: str) -> list[dict[str, Any]]:
    """Project-tier entries observed in >= _PROMOTION_MIN_SESSIONS distinct
    sessions. Deterministic, no LLM. Skips INDEX.md."""
    entries_dir = Path(store_dir) / "entries"
    out: list[dict[str, Any]] = []
    if not entries_dir.exists():
        return out
    for f in sorted(entries_dir.glob("*.md")):
        if f.name == "INDEX.md":
            continue
        e = parse_frontmatter_file(f)
        if e and _distinct_sessions(e["meta"]) >= _PROMOTION_MIN_SESSIONS:
            out.append(e)
    return out
