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


# ---------------------------------------------------------------------------
# K=3 consensus panel
# ---------------------------------------------------------------------------

_PANEL_SYSTEM = (
    "You judge whether ONE computational-chemistry project learning should become "
    "a durable RULE. Approve ONLY if it is (a) correct — following it would not be "
    "wrong — and (b) general — stated as guidance, not bolted to one peptide, "
    "system, or single run. Be conservative; most learnings are NOT rule-worthy. "
    'Return JSON: {"approve": bool, "correctness_concern": str|null, '
    '"generality_concern": str|null}. Set correctness_concern only if following '
    "the rule could produce a wrong result."
)


def _default_judge(entry: dict[str, Any], lens_idx: int) -> dict[str, Any] | None:
    from compchem_memory.llm import call_llm_json
    payload = {"title": entry["meta"].get("title", ""), "body": entry["body"][:1500]}
    return call_llm_json(_PANEL_SYSTEM, json.dumps(payload), max_tokens=400,
                         temperature=_PROMOTION_PANEL_TEMPERATURE)


def run_panel(
    entry: dict[str, Any],
    judge: Callable[[dict[str, Any], int], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Run K independent passes. survives iff >= _PROMOTION_PANEL_APPROVE approve.
    Any pass raising a correctness_concern is surfaced (correctness veto/flag)."""
    judge = judge or _default_judge
    passes: list[dict[str, Any]] = []
    for k in range(_PROMOTION_PANEL_PASSES):
        v = judge(entry, k) or {"approve": False, "correctness_concern": None,
                                 "generality_concern": None}
        passes.append(v)
    approvals = sum(1 for v in passes if v.get("approve"))
    correctness_flag = next((v.get("correctness_concern") for v in passes
                             if v.get("correctness_concern")), None)
    return {
        "passes": passes,
        "approvals": approvals,
        "survives": approvals >= _PROMOTION_PANEL_APPROVE,
        "correctness_flag": correctness_flag,
    }


# ---------------------------------------------------------------------------
# Rule drafting
# ---------------------------------------------------------------------------

import re as _re

_DRAFT_SYSTEM = (
    "Rewrite ONE project learning as a durable RULE for a computational-chemistry "
    "agent. Keep it faithful to the source — do not invent facts. Make it "
    "prescriptive and general. Return JSON: {\"name\": kebab-case slug, "
    "\"description\": one line, \"tags\": [str], \"body\": markdown guidance}."
)


def _slug(text: str) -> str:
    s = _re.sub(r"[^a-z0-9]+", "-", (text or "rule").lower()).strip("-")
    return s or "rule"


def _default_drafter(entry: dict[str, Any]) -> dict[str, Any] | None:
    from compchem_memory.llm import call_llm_json
    payload = {"title": entry["meta"].get("title", ""), "body": entry["body"],
               "tags": entry["meta"].get("tags") or []}
    res = call_llm_json(_DRAFT_SYSTEM, json.dumps(payload), max_tokens=1200,
                        temperature=0)
    return res if isinstance(res, dict) else None


def draft_rule(
    entry: dict[str, Any],
    drafter: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Draft a rule preview from a project entry. On drafter failure, fall back to
    a faithful copy (title→name, entry body verbatim) so a proposal still forms."""
    drafter = drafter or _default_drafter
    d = drafter(entry) or {}
    title = entry["meta"].get("title", "")
    return {
        "name": _slug(d.get("name") or title),
        "description": d.get("description") or title,
        "tags": d.get("tags") or entry["meta"].get("tags") or [],
        "body": d.get("body") or entry["body"],
    }


# ---------------------------------------------------------------------------
# Consistency check vs existing skill-tier rules
# ---------------------------------------------------------------------------

_CONSISTENCY_SYSTEM = (
    "Given a DRAFT rule and a list of EXISTING rules (name + description), decide "
    "whether the draft duplicates or contradicts any existing rule. Return JSON: "
    '{"status": "ok"|"duplicate"|"conflict", "related_rule": name|null, '
    '"note": one line}.'
)


def _existing_rule_summaries(skills_dir: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    d = Path(skills_dir)
    if not d.exists():
        return out
    for f in sorted(d.glob("*.md")):
        e = parse_frontmatter_file(f)
        if e:
            out.append({"name": e["meta"].get("name", f.stem),
                        "description": e["meta"].get("description", "")})
    return out


def _default_checker(drafted, rules):
    from compchem_memory.llm import call_llm_json
    payload = {"draft": {"name": drafted["name"], "description": drafted["description"]},
               "existing": rules}
    return call_llm_json(_CONSISTENCY_SYSTEM, json.dumps(payload), max_tokens=300,
                         temperature=0)


def check_consistency(
    drafted: dict[str, Any], rules: list[dict[str, str]],
    checker: Callable[[dict, list], dict | None] | None = None,
) -> dict[str, Any]:
    """Flag duplicate/conflict vs existing rules. duplicate/conflict are SURFACED,
    never auto-dropped. Defaults to 'ok' if the checker fails."""
    checker = checker or _default_checker
    res = checker(drafted, rules) or {}
    status = res.get("status")
    if status not in ("ok", "duplicate", "conflict"):
        status = "ok"
    return {"status": status, "related_rule": res.get("related_rule"),
            "note": res.get("note", "")}
