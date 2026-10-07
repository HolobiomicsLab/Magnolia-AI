"""Judge certification (2026-10-07): replay the production consolidation judge
(deepseek-flash, current _CLUSTER_SYSTEM incl. the 2026-10-06 hardening)
against the HUMAN label store (reflex/labels.jsonl) and measure agreement.

Per labeled pair: rebuild the two-item payload exactly as cluster_findings
would send it (id/title/gist[:200]/type/session), call _default_clusterer,
and record whether the judge groups the pair and at what confidence.

Metrics against the floor (0.65) and auto band (0.8):
- human REJECTED pairs: certified-agree if the judge does not propose them
  above the floor (no cluster or conf < 0.65); miss = conf >= 0.65 (would
  reach the human queue); worst = conf >= 0.8 (would have AUTO-merged a
  human-rejected pair).
- human ACCEPTED pairs: agree if conf >= 0.65.

Pairs whose sources no longer exist (merged/moved away) are skipped and
counted as unverifiable. Writes results.jsonl + REPORT.md into the run dir.
"""

import json
import sys
from pathlib import Path

STORE = Path("projects/xiulian/.magnolia")


def _entry_payload(path: Path):
    """Same compact payload shape as cluster_findings."""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            import yaml
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            body = parts[2]
    return {
        "id": path.name,
        "title": meta.get("title", ""),
        "gist": body.strip()[:200],
        "type": meta.get("type"),
        "session": meta.get("opencode_session_id"),
    }


def _find_source(store: Path, name: str) -> Path | None:
    for sub in ("staging", "entries"):
        p = store / sub / name
        if p.exists():
            return p
    return None


def main() -> int:
    from compchem_memory.consolidation import _default_clusterer

    labels_path = STORE / "reflex" / "labels.jsonl"
    rows = [json.loads(l) for l in labels_path.read_text().splitlines()]
    pairs = []
    for r in rows:
        srcs = r.get("sources") or []
        if len(srcs) >= 2:
            pairs.append(r)

    run_dir = Path("projects/xiulian/runs/2026-10-07_judge-certification")
    run_dir.mkdir(parents=True, exist_ok=True)
    out_f = open(run_dir / "results.jsonl", "w")

    stats = {"total": 0, "skipped_missing_source": 0}
    agree_reject = miss_reject = worst_reject = 0
    agree_accept = miss_accept = 0

    for i, r in enumerate(pairs):
        names = sorted(r.get("cluster_key") or [Path(s).name for s in r["sources"]])
        paths = [_find_source(STORE, n) for n in names]
        verdict = r.get("verdict")
        human_conf = r.get("confidence")
        if any(p is None for p in paths):
            stats["skipped_missing_source"] += 1
            out_f.write(json.dumps({"cluster_key": names, "verdict": verdict,
                                    "status": "skipped_missing_source"}) + "\n")
            continue
        payload = [_entry_payload(p) for p in paths]
        clusters = _default_clusterer(payload)
        proposed = None
        for c in clusters or []:
            ids = sorted(c.get("ids", []))
            if ids == names and len(ids) >= 2:
                proposed = float(c.get("confidence", 0.0))
                break
        stats["total"] += 1
        if verdict == "reject":
            if proposed is None or proposed < 0.65:
                agree_reject += 1
                cls = "agree_reject"
            elif proposed >= 0.8:
                worst_reject += 1
                cls = "worst_reject"
            else:
                miss_reject += 1
                cls = "miss_reject"
        else:
            if proposed is not None and proposed >= 0.65:
                agree_accept += 1
                cls = "agree_accept"
            else:
                miss_accept += 1
                cls = "miss_accept"
        out_f.write(json.dumps({
            "cluster_key": names, "verdict": verdict,
            "human_confidence": human_conf, "judge_confidence": proposed,
            "class": cls,
        }) + "\n")
        print(f"[{i+1}/{len(pairs)}] {cls} human={verdict}@{human_conf} judge={proposed}", flush=True)

    out_f.close()
    rep = run_dir / "REPORT.md"
    rep.write_text(f"""# Judge certification report — 2026-10-07

Judge: deepseek-flash via consolidation._default_clusterer (current hardened prompt).
Labels: {len(rows)} rows / {len(pairs)} pairs from reflex/labels.jsonl.

| class | n |
|---|---|
| human-rejected, judge suppresses (agree) | {agree_reject} |
| human-rejected, judge proposes <0.8 (miss: reaches queue) | {miss_reject} |
| human-rejected, judge proposes >=0.8 (worst: would auto-merge) | {worst_reject} |
| human-accepted, judge proposes >=0.65 (agree) | {agree_accept} |
| human-accepted, judge below floor (miss) | {miss_accept} |
| unverifiable (source gone) | {stats['skipped_missing_source']} |

Total evaluated: {stats['total']}.
""")
    print(rep.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
