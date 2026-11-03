"""NK2R peptide-design funnel — first executable stages.

PROVISIONAL prototype (choices subject to change). Runs the cheap end of the
campaign funnel locally, stdlib only, deterministic under a fixed seed:

    generate  -> three independent candidate generators (motif / mutagenesis /
                 guided-random), deduplicated
    filter    -> cheap physicochemical + liability filters, with a kill ledger
    calibrate -> known-answer control set (NKB wild-type vs scrambled/reversed/
                 unrelated negatives) that the ranking heuristic MUST separate
    rank      -> pre-registered selection: every generator represented, none
                 above 50% of the final set

HONEST SCOPE: the heuristic calibrates the FUNNEL (does the instrument rank a
real NK2R agonist motif above decoys?), not NK2R binding. Binding claims are
gated behind the docking stages, which need the NK2R structure and are not
implemented here. Nothing in this file predicts EC50.

Usage (from opencode_cc_mem/):
    python3 nk2r_challenge/src/nk2r_funnel.py run --seed 20261103 \
        --out nk2r_challenge/runs/<run_id>
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path

# --- reference pharmacology (tachykinin family) ---------------------------
NKB = "DMHDFFVGLM"  # neurokinin B, endogenous NK2R agonist, C-terminal amide
NKA = "HKTDSFVGLM"  # neurokinin A (NK1R-preferring), selectivity control
SUBSTANCE_P = "RPKPQQFFGLM"  # NK1R agonist, selectivity control
C_TERMINAL_AMIDE = True  # tachykinins are C-terminally amidated

HYDROPHOBIC = set("AVILMFWY")
POSITIVE = set("KR")
NEGATIVE = set("DE")
AROMATIC = set("FYW")
CONSERVATIVE = {
    "D": "EN", "M": "LIV", "H": "NQ", "F": "YW", "V": "IL",
    "G": "AS", "L": "IVM", "N": "QD", "Q": "NE", "S": "TN",
    "K": "R", "R": "K", "T": "S", "A": "GS", "W": "FY", "Y": "FW",
}


def net_charge(seq: str) -> int:
    return sum(aa in POSITIVE for aa in seq) - sum(aa in NEGATIVE for aa in seq)


def hydrophobic_fraction(seq: str) -> float:
    return sum(aa in HYDROPHOBIC for aa in seq) / len(seq)


def max_homopolymer(seq: str) -> int:
    best = run = 1
    for i in range(1, len(seq)):
        run = run + 1 if seq[i] == seq[i - 1] else 1
        best = max(best, run)
    return best


def pharmacophore_score(seq: str) -> float:
    """Transparent tachykinin-motif heuristic. NOT a binding model.

    +3 C-terminal [FWY][FV]GLM motif; +1 aromatic at position -6; +1 C-amide;
    +0.5 net charge in [-2, +2]; +0.5 hydrophobic fraction in [0.3, 0.6].
    """
    score = 0.0
    if len(seq) >= 5 and seq[-5] in AROMATIC and seq[-4] in "FV" and seq[-3:] == "GLM":
        score += 3.0
    if len(seq) >= 6 and seq[-6] in AROMATIC:
        score += 1.0
    if C_TERMINAL_AMIDE:
        score += 1.0
    if -2 <= net_charge(seq) <= 2:
        score += 0.5
    if 0.3 <= hydrophobic_fraction(seq) <= 0.6:
        score += 0.5
    return score


# --- generators ------------------------------------------------------------

def gen_motif(seed: int, n: int) -> list[str]:
    """G1: tachykinin-motif analogs around the NKB pharmacophore."""
    rng = random.Random(seed)
    out = set()
    nterm = ["DMHD", "DHMD", "DMND", "DQHD", "EMHD", "DMHD", "DMHE", "DMQD"]
    attempts = 0
    while len(out) < n and attempts < n * 100:
        attempts += 1
        core = rng.choice(nterm)
        f5 = rng.choice("FFY")
        x6 = rng.choice("FV")
        tail = rng.choice(["GLM", "GLM", "GLI", "GLV"])
        out.add(core + f5 + x6 + tail)
    return sorted(out)


def gen_mutagenesis(seed: int, n: int) -> list[str]:
    """G2: conservative substitutions of NKB (non-motif positions)."""
    rng = random.Random(seed + 1)
    out = set()
    positions = [0, 1, 2, 3, 6]  # D M H D ... V (motif F5 F6 G8 L9 M10 preserved)
    attempts = 0
    while len(out) < n and attempts < n * 100:
        attempts += 1
        seq = list(NKB)
        for pos in rng.sample(positions, k=rng.choice([1, 1, 2, 2, 3])):
            seq[pos] = rng.choice(CONSERVATIVE[seq[pos]])
        out.add("".join(seq))
    return sorted(out)


def gen_guided_random(seed: int, n: int) -> list[str]:
    """G3: composition-constrained random 10-mers with the C-terminal anchor."""
    rng = random.Random(seed + 2)
    out = set()
    alphabet = "ADEGHILMNPQRSTVWY"  # no Cys (disulfide liability), no Lys-free bias
    attempts = 0
    while len(out) < n and attempts < n * 100:
        attempts += 1
        head = "".join(rng.choice(alphabet) for _ in range(5))
        seq = head + rng.choice("FYW") + rng.choice("FV") + "GLM"
        if 0.3 <= hydrophobic_fraction(seq) <= 0.65:
            out.add(seq)
    return sorted(out)


GENERATORS = {"motif": gen_motif, "mutagenesis": gen_mutagenesis, "guided_random": gen_guided_random}


# --- filters ---------------------------------------------------------------

def filter_reason(seq: str) -> str | None:
    if not 8 <= len(seq) <= 12:
        return "length_outside_8_12"
    if "C" in seq:
        return "cysteine_disulfide_liability"
    if not (-2 <= net_charge(seq) <= 2):
        return "net_charge_outside_window"
    if max_homopolymer(seq) > 3:
        return "homopolymer_run_gt_3"
    if seq == NKB:
        return "wildtype_reserved_as_control"
    return None


# --- pipeline --------------------------------------------------------------

def build_library(seed: int, per_generator: int) -> dict:
    raw, killed = {}, []
    for name, fn in GENERATORS.items():
        for seq in fn(seed, per_generator):
            raw.setdefault(seq, name)  # first generator to propose keeps credit
    deduped = len(raw)
    kept = {}
    for seq, gen in raw.items():
        reason = filter_reason(seq)
        if reason:
            killed.append({"sequence": seq, "generator": gen, "reason": reason})
        else:
            kept[seq] = gen
    return {"kept": kept, "killed": killed, "deduped": deduped}


def select_ranked(kept: dict, target_n: int) -> list[dict]:
    """Round-robin by score across generators; no generator may exceed 50%."""
    by_gen = {}
    for seq, gen in kept.items():
        by_gen.setdefault(gen, []).append(seq)
    for seqs in by_gen.values():
        seqs.sort(key=lambda s: (-pharmacophore_score(s), s))
    ranked, counts = [], Counter()
    while len(ranked) < target_n and any(by_gen.values()):
        for gen in sorted(by_gen):
            if len(ranked) >= target_n:
                break
            if by_gen[gen] and counts[gen] < target_n // 2:
                seq = by_gen[gen].pop(0)
                ranked.append({"sequence": seq, "generator": gen, "score": pharmacophore_score(seq)})
                counts[gen] += 1
    return ranked


def calibration_controls() -> dict:
    rng = random.Random(7)
    scrambled = list(NKB)
    rng.shuffle(scrambled)
    positives = {"NKB_wt": NKB, "NKA_control": NKA, "SubstanceP_control": SUBSTANCE_P}
    negatives = {
        "NKB_scrambled": "".join(scrambled),
        "NKB_reversed": NKB[::-1],
        "KFERQ_unrelated": "KFERQ",
        "polyG_negative": "GGGGGGGGGG",
    }
    return {"positives": positives, "negatives": negatives}


def run_calibration() -> dict:
    controls = calibration_controls()
    pos = {k: pharmacophore_score(v) for k, v in controls["positives"].items()}
    neg = {k: pharmacophore_score(v) for k, v in controls["negatives"].items()}
    pos_median = sorted(pos.values())[len(pos) // 2]
    neg_max = max(neg.values())
    passed = pos["NKB_wt"] > neg_max and pos_median > neg_max
    return {
        "passed": passed,
        "rule": "NKB_wt score > every negative score AND positive median > negative max",
        "positive_scores": pos,
        "negative_scores": neg,
        "note": "calibrates the funnel heuristic only; NOT an NK2R binding validation",
    }


def run(seed: int, per_generator: int, target_n: int, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    lib = build_library(seed, per_generator)
    ranked = select_ranked(lib["kept"], target_n)
    calib = run_calibration()
    shares = Counter(r["generator"] for r in ranked)
    acceptance = {
        "three_generators_present": len(shares) == 3,
        "no_generator_above_50pct": all(c <= target_n // 2 for c in shares.values()),
        "calibration_passed": calib["passed"],
        "target_n_reached": len(ranked) == target_n,
    }
    record = {
        "run_id": out_dir.name,
        "seed": seed,
        "stages": {
            "generate": {"raw_unique": lib["deduped"], "per_generator_requested": per_generator},
            "filter": {"kept": len(lib["kept"]), "killed": len(lib["killed"])},
            "calibrate": calib,
            "rank": {"selected": len(ranked), "generator_shares": dict(shares)},
        },
        "acceptance": acceptance,
        "honest_negative_clause": (
            "if any acceptance item is false, the run is reported as a failed "
            "instrument check, not as a peptide-design result"
        ),
        "gated_stages_not_run": ["dock_prepare", "dock", "selectivity", "package"],
        "gating_reason": "NK2R structure + active-site definition + real docking backend required",
    }
    (out_dir / "run-record.json").write_text(json.dumps(record, indent=2))
    (out_dir / "kill-ledger.json").write_text(json.dumps(lib["killed"], indent=2))
    (out_dir / "calibration.json").write_text(json.dumps(calib, indent=2))
    with (out_dir / "ranked_candidates.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["rank", "sequence", "generator", "score"])
        writer.writeheader()
        for i, row in enumerate(ranked, 1):
            writer.writerow({"rank": i, **row})
    print(json.dumps({"acceptance": acceptance, "selected": len(ranked),
                      "shares": dict(shares), "kept": len(lib["kept"]),
                      "killed": len(lib["killed"]), "out": str(out_dir)}, indent=2))
    return 0 if all(acceptance.values()) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--seed", type=int, default=20261103)
    run_p.add_argument("--per-generator", type=int, default=120)
    run_p.add_argument("--target-n", type=int, default=60)
    run_p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        return run(args.seed, args.per_generator, args.target_n, args.out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
