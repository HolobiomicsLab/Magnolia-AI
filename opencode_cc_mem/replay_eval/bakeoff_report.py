"""Bake-off report: the five pre-registered metrics (TEST-PLAN.md 2026-09-30).

    python -m replay_eval.bakeoff_report --arm1 <dir> --arm2 <dir> \
        [--corpus replay_eval/corpus/hsc70_bakeoff]

Reads only data on disk: the two run dirs' summary.json, labels.json, and the
LIVE hsc70 store (read-only) for entry titles. No LLM calls.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_WORD_RE = re.compile(r"[a-z0-9]+")
_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.M)


def toks(title: str) -> set[str]:
    return {t for t in _WORD_RE.findall((title or "").lower()) if len(t) >= 3 and not t.isdigit()}


def jac(a: str, b: str) -> float:
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def conj(a: str, b: str) -> bool:
    """TEST-PLAN match rule: token conjunction >= 0.6 with >= 2 shared."""
    ta, tb = toks(a), toks(b)
    shared = ta & tb
    return len(shared) >= 2 and bool(tb) and len(shared) / min(len(ta), len(tb)) >= 0.6


def entry_title(store: Path, fname: str) -> str:
    for sub in ("entries", "staging"):
        p = store / sub / fname
        if p.exists():
            m = _TITLE_RE.search(p.read_text(encoding="utf-8", errors="replace")[:2000])
            if m:
                return m.group(1)
    return ""


def load(run: Path) -> dict[str, list[str]]:
    s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    return {r["slice"]: [c.get("title", "") for c in r.get("candidates", [])]
            for r in s["records"]}, s


def historical_titles_by_sid(store: Path) -> dict[str, set[str]]:
    """Arm-0 candidate titles per session, from staging/entries provenance."""
    out: dict[str, set[str]] = {}
    for sub in ("staging", "entries"):
        for f in (store / sub).glob("*.md"):
            head = f.read_text(encoding="utf-8", errors="replace")[:2000]
            sid_m = re.search(r"^opencode_session_id:\s*(ses_\S+)", head, re.M)
            t_m = _TITLE_RE.search(head)
            if sid_m and t_m:
                out.setdefault(sid_m.group(1), set()).add(t_m.group(1))
    return out


def greedy_jaccard(a_titles: list[str], b_titles: list[str]) -> tuple[float, float]:
    """One-to-one greedy matching: (avg Jaccard of matched pairs, matched frac of A)."""
    pairs = sorted(
        ((jac(a, b), i, j) for i, a in enumerate(a_titles) for j, b in enumerate(b_titles)),
        reverse=True)
    used_i, used_j, matched, total = set(), set(), 0.0, 0
    for score, i, j in pairs:
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        matched += score
        total += 1
    if total == 0:
        return 0.0, 0.0
    return matched / total, total / max(len(a_titles), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm1", required=True)
    ap.add_argument("--arm2", required=True)
    ap.add_argument("--corpus", default="replay_eval/corpus/hsc70_bakeoff")
    a = ap.parse_args()

    store = Path("/home/tjiang/repos/project_magnolia/opencode_cc_mem/projects/hsc70_new/.magnolia")
    labels = json.loads((Path(a.corpus) / "labels.json").read_text(encoding="utf-8"))
    cur = labels["curated"]["clean"]
    workhorses = cur["workhorses"]
    never = cur["never_surfaced"]

    a1_by_slice, s1 = load(Path(a.arm1))
    a2_by_slice, s2 = load(Path(a.arm2))
    # v4 corpus: slice names are <sid> or <sid>__kN — group titles by session
    def by_sid(d):
        out: dict[str, list[str]] = {}
        for name, titles in d.items():
            out.setdefault(name.split("__")[0], []).extend(titles)
        return out
    a1, a2 = by_sid(a1_by_slice), by_sid(a2_by_slice)

    # 1. Fidelity: Arm-1 vs historical candidates, same sessions
    hist = historical_titles_by_sid(store)
    scores, fracs = [], []
    for sid, titles in a1.items():
        h = sorted(hist.get(sid, []))
        if h and titles:
            j, fr = greedy_jaccard(titles, sorted(h))
            scores.append(j)
            fracs.append(fr)
    fidelity = sum(scores) / len(scores) if scores else 0.0

    # Arm-2 admitted titles (chronological = record order)
    a2_titles = [t for titles in a2.values() for t in titles]

    # 2. Workhorse recall
    wh_titles = [(f, entry_title(store, f)) for f in workhorses]
    recalled = [f for f, t in wh_titles if t and any(conj(t, c) for c in a2_titles)]
    recall = len(recalled) / len(wh_titles) if wh_titles else 0.0

    # 3. Volume
    volume = s2["total_candidates"] / max(s1["total_candidates"], 1)

    # 4. Dead-weight rejection
    never_titles = [(f, entry_title(store, f)) for f in never]
    kept = [f for f, t in never_titles if t and any(conj(t, c) for c in a2_titles)]
    dead_rej = 1 - len(kept) / len(never_titles) if never_titles else 0.0

    # 5. Duplicate inflow within Arm 2 (chronological)
    dups = [(i, t1, t2) for i, (t1, t2) in enumerate(zip(a2_titles, a2_titles[1:]))
            if conj(t1, t2)]
    dups += [(i, t1, t2) for i, (t1, t2) in
             [(k, (a2_titles[k], a2_titles[j])) for k in range(len(a2_titles))
              for j in range(k + 2, min(k + 6, len(a2_titles)))] if conj(t1, t2)]

    print("=== hsc70 bake-off report (pre-registered TEST-PLAN 2026-09-30) ===")
    print(f"1. fidelity  Arm1-vs-history avg Jaccard = {fidelity:.2f} "
          f"over {len(scores)} sessions (gate >= 0.6)  -> "
          f"{'PASS' if fidelity >= 0.6 else 'FAIL'}")
    print(f"2. recall    workhorses {len(recalled)}/{len(wh_titles)} = {recall:.0%} "
          f"(gate >= 95%)  -> {'PASS' if recall >= 0.95 else 'FAIL'}")
    if recall < 0.95:
        missing = [f for f, t in wh_titles if f not in recalled][:10]
        print(f"   missing examples: {missing}")

    # 2b. Semantic recall (bge-m3, 2026-10-07): reported alongside the
    # pre-registered lexical gate, never replacing it without a TEST-PLAN
    # re-registration (Goodhart guard). The lexical matcher is a measured
    # floor: it cannot pair same-learning/different-title candidates
    # (RECALL-ANALYSIS.md in the arm dir; 10/10 lexical misses recovered by
    # content-cosine).
    sem_file = Path(a.arm2) / "RECALL-SEMANTIC.json"
    if sem_file.exists():
        try:
            sem = json.loads(sem_file.read_text(encoding="utf-8"))
            sim_by_name = {r["entry"]: r["sim"] for r in
                           sem.get("recalled", []) + sem.get("missing", [])}
            sem_recalled = [f for f, t in wh_titles
                            if sim_by_name.get(f, 0) >= sem.get("threshold", 0.6)]
            sem_recall = (len(sem_recalled) / len(wh_titles)) if wh_titles else 0.0
            band_05 = sum(1 for f, t in wh_titles
                          if 0.5 <= sim_by_name.get(f, 0) < sem.get("threshold", 0.6))
            print(f"2b. recall (semantic, {sem.get('mode', 'bge-m3')}) "
                  f"workhorses {len(sem_recalled)}/{len(wh_titles)} = {sem_recall:.0%} "
                  f"at threshold {sem.get('threshold', 0.6)}  -> "
                  f"{'PASS' if sem_recall >= 0.95 else 'FAIL'}")
            if band_05:
                print(f"    (plus {band_05} workhorse(s) in the 0.5-0.6 band)")
            if sem_recall >= 0.95 and recall < 0.95:
                print("    note: lexical gate FAIL is a scorer artifact — "
                      "content-cosine recovers the lexical misses; re-register "
                      "the metric in TEST-PLAN before treating this arm as failed.")
        except Exception as e:
            print(f"2b. semantic recall unavailable: {e}")
    print(f"3. volume    {s2['total_candidates']}/{s1['total_candidates']} = "
          f"{volume:.1%} (gate <= 40%)  -> {'PASS' if volume <= 0.40 else 'FAIL'}")
    print(f"4. dead-weight rejection {dead_rej:.0%} (gate >= 70%)  -> "
          f"{'PASS' if dead_rej >= 0.70 else 'FAIL'}")
    print(f"5. dup inflow {len(dups)} same-claim pairs (gate ~0)  -> "
          f"{'PASS' if len(dups) == 0 else 'CHECK'}")
    verdict = (fidelity >= 0.6 and recall >= 0.95 and volume <= 0.40
               and dead_rej >= 0.70 and len(dups) == 0)
    print(f"\nOVERALL: {'PASS — admission line may phase-gate to master' if verdict else 'FAIL — iterate before merge'}")
    # Exit code mirrors the verdict: a chained `run && judge && report` must
    # NOT be green on a failing arm (2026-10-08: an invalid arm — wrong
    # corpus, judge 0% completeness — exited 0 end to end).
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
