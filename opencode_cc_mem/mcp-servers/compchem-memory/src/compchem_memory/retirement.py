"""Retirement v2 (exposure rule, 2026-10-06): staging entries that never proved
useful move out of the staging pool — non-destructively, into <store>/retired/
(git-reversible) — so the stock trends toward corroborated, surfaced knowledge.

Eligibility (ALL must hold):
  - observation_count < 3                  (hard floor: corroborated entries never retire)
  - surfaced in <= 1 distinct session       (hard floor: repeatedly injected entries never
                                            retire; derived from action-retrieval.jsonl)
  - >= MIN_OPPORTUNITIES retrieval opportunities (memory_get_context calls, from
    reflex/exposure.jsonl) since the entry was created, spanning >= 3 distinct
    days — the "fair chance" rule: an entry that never had E chances to prove
    itself has not been evaluated yet.

Velocity cap per sweep: min(20, 5% of staging count), oldest entries first.
Gated behind MAGNOLIA_RETIREMENT (default off) at the sweep site; every run
writes a receipt row to reflex/retirement-log.jsonl and rides the
.distill-notices queue — a store rewrite the user never saw is the failure
mode this project distrusts.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.reflex_common import parse_frontmatter_file

RETIRED_DIR = "retired"
MIN_OPPORTUNITIES_DEFAULT = 10
MIN_OPPORTUNITY_DAYS = 3
VELOCITY_CAP_DEFAULT = 20
VELOCITY_SHARE = 0.05


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _min_opportunities() -> int:
    try:
        return int(os.environ.get(
            "MAGNOLIA_RETIREMENT_MIN_OPPORTUNITIES", MIN_OPPORTUNITIES_DEFAULT))
    except (TypeError, ValueError):
        return MIN_OPPORTUNITIES_DEFAULT


def record_opportunity(project_dir: str) -> None:
    """Append one retrieval-opportunity row to the exposure ledger. One
    memory_get_context call = one opportunity for every staging entry alive at
    that moment. Pure write-side log; never raises."""
    try:
        led = Path(project_dir) / ".magnolia" / "reflex" / "exposure.jsonl"
        led.parent.mkdir(parents=True, exist_ok=True)
        with open(led, "a") as f:
            f.write(json.dumps({"ts": _now(), "kind": "retrieval_opportunity"}) + "\n")
    except Exception as e:  # noqa: BLE001 - the ledger must never break retrieval
        print(f"[retirement] exposure ledger skipped: {e}")


def _load_opportunities(store: Path) -> list[datetime]:
    led = store / "reflex" / "exposure.jsonl"
    if not led.exists():
        return []
    out: list[datetime] = []
    try:
        for line in led.read_text().splitlines():
            try:
                ts = datetime.fromisoformat(json.loads(line)["ts"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
            out.append(ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))
    except OSError:
        return []
    return out


def _created_dt(meta: dict, path: Path) -> datetime | None:
    raw = meta.get("created") or ""
    try:
        dt = datetime.fromisoformat(str(raw).strip("'\"")[:19])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None


def _surfaced_sessions(store: Path, filename: str, title: str = "") -> set[str]:
    """Distinct sessions in which the entry was actually injected, derived from
    the action-retrieval log: a row surfaces the entry when its filename OR its
    title appears in the row (tolerant across the v1 titles-only and v2
    path+tier row schemas). Title matching errs safe — a false positive only
    protects an entry from retirement. Missing log -> empty set."""
    log = store / "action-retrieval.jsonl"
    if not log.exists():
        return set()
    needles = [n for n in (filename, (title or "").strip()) if n]
    out: set[str] = set()
    try:
        for line in log.read_text(errors="replace").splitlines():
            if not any(n in line for n in needles):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = row.get("sessionID") or row.get("sessionId") or ""
            if sid:
                out.add(str(sid))
    except OSError:
        return set()
    return out


def retirement_candidates(store: Path) -> list[dict]:
    """Staging entries meeting ALL eligibility criteria, oldest-created first.
    Purely read-only — the sweep decides and caps."""
    opportunities = _load_opportunities(store)
    min_opp = _min_opportunities()
    out: list[dict] = []
    for f in sorted(Path(store, "staging").glob("*.md")):
        if f.name == "INDEX.md":
            continue
        try:
            e = parse_frontmatter_file(f)
        except Exception:  # noqa: BLE001 - unreadable entry: skip, don't abort
            continue
        if not e:
            continue
        meta = e["meta"]
        if meta.get("parked"):
            continue  # parked entries are retirement-immune (R9)
        obs = meta.get("observation_count", 0) or 0
        if obs >= 3:
            continue  # hard floor: corroborated entries never retire
        surfaced = _surfaced_sessions(store, f.name, str(meta.get("title", "")))
        if len(surfaced) >= 2:
            continue  # hard floor: repeatedly injected entries never retire
        created = _created_dt(meta, f)
        if created is None:
            continue
        opps = [ts for ts in opportunities if ts >= created]
        days = {ts.date() for ts in opps}
        if len(opps) < min_opp or len(days) < MIN_OPPORTUNITY_DAYS:
            continue  # fair-chance rule: not enough evidence yet
        out.append({"path": f, "meta": meta, "obs": obs,
                    "surfaced": len(surfaced), "opportunities": len(opps),
                    "days": len(days), "created": created})
    out.sort(key=lambda c: c["created"])
    return out


def retire_eligible(project_dir: str) -> dict:
    """Move retirement candidates out of staging under the velocity cap
    (oldest first), writing one receipt row per retired entry. Returns
    {"retired": [names], "candidates": int, "cap": int, "skipped_reason"?: str}.
    Never raises beyond the caller's guard."""
    from compchem_memory import canary

    pd = Path(project_dir)
    store = pd / ".magnolia"
    if canary.is_frozen(str(pd)):
        return {"retired": [], "candidates": 0, "cap": 0,
                "skipped_reason": "canary_frozen"}
    cands = retirement_candidates(store)
    staging_n = len(list((store / "staging").glob("*.md")))
    cap = min(VELOCITY_CAP_DEFAULT, max(1, int(staging_n * VELOCITY_SHARE)))
    chosen = cands[:cap]
    if not chosen:
        return {"retired": [], "candidates": 0, "cap": cap}
    retired_dir = store / RETIRED_DIR
    retired_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    try:
        log = store / "reflex" / "retirement-log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as lf:
            for c in chosen:
                dest = retired_dir / c["path"].name
                c["path"].rename(dest)
                moved.append(c["path"].name)
                lf.write(json.dumps({
                    "ts": _now(), "entry": c["path"].name,
                    "title": c["meta"].get("title", ""),
                    "obs": c["obs"], "surfaced_sessions": c["surfaced"],
                    "opportunities": c["opportunities"], "days": c["days"],
                    "destination": str(dest),
                }) + "\n")
    except OSError as e:
        print(f"[retirement] partially applied, receipt skipped: {e}")
    return {"retired": moved, "candidates": len(cands), "cap": cap}
