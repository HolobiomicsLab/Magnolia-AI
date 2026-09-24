"""Baseline analysis of action-time injection logs (plan D1/D2, 2026-09-18).

Read-only summarizer for `<project>/.magnolia/action-retrieval.jsonl`:
injection volume, per-entry surfacing, tier split, outcome markers, and skip
reasons. When `--project-dir` is given, surfaced paths/titles are resolved
against the store to report per-tier coverage (how much of staging/project was
ever surfaced).
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def load_events(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    events = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _injected(e: dict) -> bool:
    if "injected" in e:
        return bool(e.get("injected"))
    return bool(e.get("hits", 0))


def application_report(events: list[dict]) -> dict:
    """P3 memory-quality telemetry: join application-probe rows (event ==
    "application", emitted by the plugin at session.idle) with the injection
    rows they refer to, keyed by (sessionID, callID).

    An injection with no probe counts as ``probes_missing`` (session ended
    before a probe, plugin reload, older schema) — it must NOT count as
    not-applied, or the rate would be biased downward. Probes with no
    matching injection row are ``orphans`` (injection logged pre-upgrade).
    The ``applied`` signal is a LEXICAL PROXY (>=2 distinctive tokens of the
    entry title/path appearing in the turn's reply), not semantic proof."""
    probes = [e for e in events if e.get("event") == "application"]
    inj_keys: set[tuple[str, str]] = set()
    for e in events:
        if _injected(e) and e.get("callID"):
            inj_keys.add((str(e.get("sessionID", "")), str(e.get("callID", ""))))

    applied = not_applied = orphans = 0
    by_tier: dict[str, dict[str, int]] = {}
    tier_by_key: dict[tuple[str, str], str] = {}
    for e in events:
        if not (_injected(e) and e.get("callID")):
            continue
        key = (str(e.get("sessionID", "")), str(e.get("callID", "")))
        tiers = {h.get("tier", "?") for h in (e.get("entries") or [])} or {"?"}
        tier_by_key[key] = "/".join(sorted(str(t) for t in tiers))

    for p in probes:
        key = (str(p.get("sessionID", "")), str(p.get("callID", "")))
        if key not in inj_keys:
            orphans += 1
            continue
        tier = tier_by_key.get(key, "?")
        slot = by_tier.setdefault(tier, {"applied": 0, "not_applied": 0})
        if p.get("applied"):
            applied += 1
            slot["applied"] += 1
        else:
            not_applied += 1
            slot["not_applied"] += 1

    probed = applied + not_applied
    missing = len(inj_keys) - probed
    return {
        "probes": len(probes),
        "applied": applied,
        "not_applied": not_applied,
        "application_rate": round(applied / probed, 3) if probed else None,
        "probes_missing": max(missing, 0),
        "orphans": orphans,
        "by_tier": by_tier,
    }


def summarize(events: list[dict], store_dir: str | None = None) -> dict:
    total = len(events)
    inj = [e for e in events if _injected(e)]
    by_tool = Counter(e.get("tool", "?") for e in events)
    outcomes = Counter(e.get("outcome", "?") for e in events)
    skips = Counter(e.get("skipped") for e in events if e.get("skipped"))

    path_counts: Counter = Counter()
    title_counts: Counter = Counter()
    tier_counts: Counter = Counter()
    for e in inj:
        for h in e.get("entries", []) or []:
            path_counts[h.get("path", "?")] += 1
            tier_counts[h.get("tier", "?")] += 1
        for t in e.get("titles", []) or []:
            title_counts[t] += 1

    report: dict = {
        "events": total,
        "injected_events": len(inj),
        "by_tool": dict(by_tool.most_common()),
        "outcomes": dict(outcomes.most_common()),
        "skips": dict(skips.most_common()),
        "distinct_paths_surfaced": len(path_counts),
        "surfaced_by_path": dict(path_counts.most_common(10)),
        "surfaced_by_title": dict(title_counts.most_common(10)),
        "application": application_report(events),
    }

    if store_dir:
        store = Path(store_dir)
        tier_of_title: dict[str, str] = {}
        totals: Counter = Counter()
        for tier in ("entries", "staging"):
            d = store / ".magnolia" / tier
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                if f.name == "INDEX.md" or f.name.startswith("._"):
                    continue
                totals[tier] += 1
                m = re.search(r"^title:\s*(.+?)$", f.read_text(errors="replace"), re.M)
                if m:
                    tier_of_title[m.group(1).strip().strip("'\"")] = tier
        for p in path_counts:
            if p in tier_of_title:
                tier_counts[tier_of_title[p]] += 1
        covered: Counter = Counter()
        seen = set()
        for t, c in title_counts.items():
            tier = tier_of_title.get(t)
            if tier and t not in seen:
                covered[tier] += 1
                seen.add(t)
        report["tier_totals"] = dict(totals)
        report["distinct_surfaced_by_tier"] = dict(tier_counts.most_common())
        report["coverage_by_tier"] = {
            tier: f"{covered.get(tier, 0)}/{totals.get(tier, 0)}"
            for tier in sorted(totals)
        }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(prog="analyze_injections")
    ap.add_argument("--project-dir", required=True, help="project dir (contains .magnolia/)")
    ap.add_argument("--log", default=None, help="action-retrieval.jsonl path override")
    args = ap.parse_args()
    pd = Path(args.project_dir)
    log = args.log or (pd / ".magnolia" / "action-retrieval.jsonl")
    report = summarize(load_events(log), store_dir=str(pd))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
