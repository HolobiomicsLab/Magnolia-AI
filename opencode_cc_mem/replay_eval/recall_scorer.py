"""Semantic recall scorer for the replay-eval bake-off (2026-10-07).

Replaces the title-conjunction matcher, which undercounts recall whenever the
replay extractor titles a learning differently from the historical workhorse
entry (measured on arm 3: lexical floor 67% vs content-cosine 94%).

Each store entry and each arm candidate (title + content) is embedded with
bge-m3 via the local ollama instance (the project's standard embedding stack).
For every entry the best-matching candidate is reported by cosine similarity;
recall is the fraction of entries with a best match >= --threshold.

Embedding backend falls back to TF-IDF cosine when ollama is unreachable, so
the scorer never hard-fails.

Usage:
    python -m replay_eval.recall_scorer --arm <run-dir> \
        [--store <hsc70_new .magnolia dir>] [--threshold 0.6] [--limit N]

The entry universe is every entry in the store (entries/ + staging/).
Restricting it to the clean-36 workhorse subset from labels.json is a TODO
once the label loader exposes that list.
"""

import argparse
import json
import math
import re
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "bge-m3"
EMBED_CHARS = 8000  # bge-m3 context headroom; longer entries are truncated
STOP = set('the a an and or of to in for with from by on at is are was were be been it its this that these those not no but if then than as we i you they he she them his her our your their there here what which who how when where why all any can could should would will may might must do does did done has have had also into onto over under out up down about above below between through during before after same other some such only own so too very s t'.split())


def toks(text):
    return [w for w in re.findall(r"[a-z0-9]{2,}", text.lower()) if w not in STOP]


def body_of(p):
    text = p.read_text(encoding="utf-8", errors="replace")
    parts = text.split("---", 2)
    return parts[2] if len(parts) == 3 else text


def title_of(p, text):
    m = re.search(r"^title:\s*(.+)$", text, re.M)
    return m.group(1) if m else p.stem


def embed_ollama(text: str, model: str = EMBED_MODEL):
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({"model": model, "prompt": text[:EMBED_CHARS]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read())
        e = d.get("embedding")
        return e if isinstance(e, list) and e else None
    except Exception:
        return None


def cos_list(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cos_dict(a, b):
    inter = set(a) & set(b)
    if not inter:
        return 0.0
    dot = sum(a[w] * b[w] for w in inter)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def tfidf_vec(text, df, n_docs):
    counts = {}
    for w in toks(text):
        counts[w] = counts.get(w, 0) + 1
    return {w: (1 + math.log(n)) * (math.log((n_docs + 1) / (df.get(w, 0) + 1)) + 1)
            for w, n in counts.items()}


def build_df(texts):
    df = {}
    for t in texts:
        for w in set(toks(t)):
            df[w] = df.get(w, 0) + 1
    return df


def main():
    ap = argparse.ArgumentParser(prog="recall_scorer")
    ap.add_argument("--arm", required=True, help="bake-off run dir with outputs/")
    ap.add_argument("--store", default="/home/tjiang/repos/project_magnolia/opencode_cc_mem/projects/hsc70_new/.magnolia")
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--limit", type=int, default=None, help="cap store entries scored")
    args = ap.parse_args()

    arm = Path(args.arm)
    store = Path(args.store)

    cands = []
    for f in sorted(arm.glob("outputs/*.json")):
        d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        for c in d.get("candidates", []):
            if isinstance(c, dict) and c.get("title"):
                cands.append({"title": c["title"],
                              "text": c["title"] + "\n" + (c.get("content") or "")})
    print(f"candidates: {len(cands)}", flush=True)

    entries = []
    for sub in ("entries", "staging"):
        for f in sorted((store / sub).glob("*.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            entries.append({"file": f, "title": title_of(f, text),
                            "text": title_of(f, text) + "\n" + body_of(f)})
    if args.limit:
        entries = entries[: args.limit]
    print(f"entries: {len(entries)}", flush=True)

    # embedding mode probe (once)
    use_embed = embed_ollama("connectivity probe") is not None
    mode = "bge-m3" if use_embed else "tfidf-fallback"
    df = build_df([c["text"] for c in cands] + [e["text"] for e in entries]) \
        if not use_embed else None
    n_docs = len(cands) + len(entries)

    def vec(text):
        if use_embed:
            e = embed_ollama(text)
            return e
        return tfidf_vec(text, df, n_docs)

    def sim(a, b):
        if a is None or b is None:
            return 0.0
        return cos_list(a, b) if use_embed else cos_dict(a, b)

    # embed candidates
    for i, c in enumerate(cands):
        c["v"] = vec(c["text"])
        if i % 200 == 0:
            print(f"  embedded candidates {i}/{len(cands)}", flush=True)

    recalled, missing = [], []
    for i, e in enumerate(entries):
        ev = vec(e["text"])
        best = (0.0, "")
        for c in cands:
            s = sim(ev, c["v"])
            if s > best[0]:
                best = (s, c["title"])
        rec = {"entry": e["file"].name, "sub": e["file"].parent.name,
               "sim": round(best[0], 3), "best_candidate": best[1]}
        (recalled if best[0] >= args.threshold else missing).append(rec)
        if i % 50 == 0:
            print(f"  scored entries {i}/{len(entries)}", flush=True)

    total = len(entries)
    summary = {"mode": mode, "threshold": args.threshold,
               "n_candidates": len(cands), "n_entries": total,
               "recalled": recalled, "missing": missing,
               "recall": round(len(recalled) / total, 3) if total else 0.0}
    out = arm / "RECALL-SEMANTIC.json"
    out.write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(f"mode={mode} recall={summary['recall']} "
          f"recalled={len(recalled)}/{total} -> {out}", flush=True)


if __name__ == "__main__":
    main()
