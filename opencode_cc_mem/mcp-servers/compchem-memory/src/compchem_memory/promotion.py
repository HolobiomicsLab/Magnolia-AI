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
    res = call_llm_json(_PANEL_SYSTEM, json.dumps(payload), max_tokens=400,
                        temperature=_PROMOTION_PANEL_TEMPERATURE)
    return res if isinstance(res, dict) else None


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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _entry_key(proposal: dict[str, Any]) -> str:
    """Content identity of a promotion proposal — the source entry basename."""
    return Path(proposal.get("source", "")).name


def propose_promotions(
    store_dir: str, *, skills_dir: str,
    judge=None, drafter=None, checker=None,
) -> dict[str, Any]:
    """Gate → panel → draft → consistency. Write survivors to
    reflex/promotion-proposal.json. Proposal-only; mutates no entries.
    Carries prior rejections forward by content key (entry basename)."""
    store = Path(store_dir)
    rules = _existing_rule_summaries(skills_dir)
    proposals: list[dict[str, Any]] = []
    for entry in eligible_entries(store_dir):
        panel = run_panel(entry, judge=judge)
        if not panel["survives"]:
            continue
        drafted = draft_rule(entry, drafter=drafter)
        consistency = check_consistency(drafted, rules, checker=checker)
        proposals.append({
            "source": entry["path"],
            "entry_title": entry["meta"].get("title", ""),
            "distinct_sessions": _distinct_sessions(entry["meta"]),
            "panel": {"approvals": panel["approvals"], "passes": panel["passes"],
                      "correctness_flag": panel["correctness_flag"]},
            "consistency": consistency,
            "drafted_rule": drafted,
            "confidence": round(panel["approvals"] / _PROMOTION_PANEL_PASSES, 2),
        })

    artifact = store / "reflex" / "promotion-proposal.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    prior_keys = prior_rejected_keys(artifact, _entry_key)
    rejected = [i for i, p in enumerate(proposals) if _entry_key(p) in prior_keys]
    artifact.write_text(json.dumps(
        {"proposals": proposals, "applied": [], "rejected": rejected}, indent=2))
    return {"candidates": len(proposals), "artifact": str(artifact)}


# ---------------------------------------------------------------------------
# Read-only review renderer
# ---------------------------------------------------------------------------


def render_promotions_markdown(store_dir: str) -> str | None:
    """Write a read-only review of UNAPPLIED promotion proposals to
    <project>/magnolia-review/promotions.md. Returns the path, or None if nothing
    is pending."""
    store = Path(store_dir)
    artifact = store / "reflex" / "promotion-proposal.json"
    if not artifact.exists():
        return None
    data = json.loads(artifact.read_text())
    proposals = data.get("proposals", [])
    pending = pending_indices(data)
    if not pending:
        return None

    lines = [
        "# Rule-elevation proposals — review",
        "",
        "Each project learning below passed the panel; the agent proposes elevating",
        "it to a cross-project rule. Tell the agent which to apply or reject "
        '(e.g. "apply 0, reject 1"). Edit the rule file AFTER it is created.',
        "",
    ]
    for i in pending:
        p = proposals[i]
        dr = p.get("drafted_rule", {})
        cons = p.get("consistency", {})
        flag = p.get("panel", {}).get("correctness_flag")
        lines += [
            f"## [{i}] {p.get('entry_title', '')}",
            "- action: accept",
            f"- confidence: {p.get('confidence')}  |  distinct sessions: "
            f"{p.get('distinct_sessions')}  |  approvals: "
            f"{p.get('panel', {}).get('approvals')}/{_PROMOTION_PANEL_PASSES}",
        ]
        if flag:
            lines.append(f"- ⚠ correctness concern: {flag}")
        if cons.get("status") in ("duplicate", "conflict"):
            lines.append(f"- ⚠ {cons['status']} of existing rule "
                         f"`{cons.get('related_rule')}`: {cons.get('note', '')}")
        lines += [
            f"- proposed rule: `{dr.get('name', '')}` — {dr.get('description', '')}",
            "",
            "<details><summary>drafted rule body</summary>",
            "",
            *fenced_preview(dr.get("body", "")),
            "</details>",
            "",
        ]

    review_dir = store.parent / "magnolia-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    out = review_dir / "promotions.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------------------
# Apply path: accept / reject / promote_raw
# ---------------------------------------------------------------------------
from compchem_memory.storage import backup_file


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _write_rule(skills_dir: str, drafted: dict[str, Any]) -> str:
    """Write a drafted rule to <skills_dir>/<name>.md with full frontmatter.
    Refuses to overwrite an existing rule file — a name collision is surfaced as a
    failed apply (never a silent clobber)."""
    meta = {"name": drafted["name"], "description": drafted.get("description", ""),
            "version": "1.0", "tags": drafted.get("tags") or [],
            "last_verified": _today()}
    Path(skills_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(skills_dir) / f"{drafted['name']}.md"
    if dest.exists():
        raise FileExistsError(f"rule already exists: {dest.name}")
    dest.write_text(
        "---\n" + yaml.dump(meta, default_flow_style=False, allow_unicode=True)
        + "---\n\n" + drafted.get("body", "").strip() + "\n", encoding="utf-8")
    return str(dest)


def _archive_entry(source: str, store_dir: str) -> None:
    """Back up then remove the project entry (it is now a rule)."""
    p = Path(source)
    if p.exists():
        backup_file(p, str(Path(store_dir).parent))
        p.unlink(missing_ok=True)


def apply_promotions(
    store_dir: str, skills_dir: str,
    accept: list[int] | None = None, reject: list[int] | None = None,
    promote_raw: list[int] | None = None,
) -> dict[str, Any]:
    """Apply accepted promotions (write drafted rule + archive source entry),
    promote_raw (elevate entry verbatim), and record rejections durably.
    Persists after each apply; one failure never aborts the batch (recorded in
    `failed`). Deterministic over the artifact — no markdown parsing."""
    store = Path(store_dir)
    artifact = store / "reflex" / "promotion-proposal.json"
    empty = {"applied": 0, "rules": [], "promoted_raw": 0, "rejected": 0, "failed": []}
    if not artifact.exists():
        return empty
    data = json.loads(artifact.read_text())
    proposals = data.get("proposals", [])
    applied_set = set(data.get("applied", []))
    rejected_set = set(data.get("rejected", []))

    def _persist():
        data["applied"] = sorted(applied_set)
        data["rejected"] = sorted(rejected_set)
        artifact.write_text(json.dumps(data, indent=2))

    rejected_n = 0
    for i in reject or []:
        if isinstance(i, int) and 0 <= i < len(proposals) and i not in applied_set and i not in rejected_set:
            rejected_set.add(i); rejected_n += 1

    rules: list[str] = []
    raw_n = 0
    failed: list[int] = []
    accept_list = accept or []
    for i in accept_list + (promote_raw or []):
        if not isinstance(i, int) or i < 0 or i >= len(proposals) or i in applied_set or i in rejected_set:
            continue
        p = proposals[i]
        try:
            if i in accept_list:
                path = _write_rule(skills_dir, p["drafted_rule"])
                is_raw = False
            else:
                e = parse_frontmatter_file(p["source"])
                if e is None:
                    failed.append(i)
                    continue
                path = _write_rule(skills_dir, {
                    "name": _slug(e["meta"].get("name") or e["meta"].get("title", "")),
                    "description": e["meta"].get("description", e["meta"].get("title", "")),
                    "tags": e["meta"].get("tags") or [], "body": e["body"]})
                is_raw = True
            _archive_entry(p["source"], store_dir)
            applied_set.add(i)
            _persist()
            rules.append(path)
            if is_raw:
                raw_n += 1
        except Exception as ex:  # noqa: BLE001 - one bad apply must not abort the batch
            print(f"[promotion] apply failed for proposal {i}: {ex}")
            failed.append(i)

    _persist()
    return {"applied": len(rules) - raw_n, "rules": rules, "promoted_raw": raw_n,
            "rejected": rejected_n, "failed": failed}
