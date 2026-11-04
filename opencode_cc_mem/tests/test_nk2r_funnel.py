"""Tests for the NK2R funnel prototype.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_nk2r_funnel.py -q
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nk2r_challenge" / "src"))

import nk2r_funnel as f


def test_library_is_deterministic_under_seed():
    a = f.build_library(seed=42, per_generator=30)
    b = f.build_library(seed=42, per_generator=30)
    assert a["kept"] == b["kept"]
    assert a["killed"] == b["killed"]


def test_three_generators_and_dedup():
    lib = f.build_library(seed=1, per_generator=40)
    assert set(lib["kept"].values()) == set(f.GENERATORS)
    assert len(lib["kept"]) == len(set(lib["kept"]))


def test_filters_reject_liabilities():
    assert f.filter_reason("CCCCCCCCCC") == "cysteine_disulfide_liability"
    assert f.filter_reason("KKKKKFFFFGLM") == "net_charge_outside_window"
    assert f.filter_reason("DDDDDDDDDD") == "net_charge_outside_window"
    assert f.filter_reason("AAAAAAAAAA") == "homopolymer_run_gt_3"
    assert f.filter_reason(f.NKB) == "wildtype_reserved_as_control"


def test_novelty_not_returned_by_motif_generator():
    lib = f.build_library(seed=3, per_generator=30)
    assert f.NKB not in lib["kept"]


def test_calibration_passes_and_nkb_beats_decoys():
    calib = f.run_calibration()
    assert calib["passed"] is True
    neg_max = max(calib["negative_scores"].values())
    assert calib["positive_scores"]["NKB_wt"] > neg_max
    # Negatives really are negative on the motif term.
    assert calib["negative_scores"]["KFERQ_unrelated"] < calib["positive_scores"]["NKB_wt"]


def test_ranked_selection_respects_generator_cap():
    lib = f.build_library(seed=5, per_generator=60)
    ranked = f.select_ranked(lib["kept"], target_n=60)
    shares = Counter(r["generator"] for r in ranked)
    assert len(shares) == 3
    assert all(c <= 30 for c in shares.values())
    assert len(ranked) == 60


def test_run_record_schema(tmp_path):
    out = tmp_path / "run1"
    code = f.run(seed=11, per_generator=40, target_n=30, out_dir=out)
    assert code == 0
    record = json.loads((out / "run-record.json").read_text())
    assert record["acceptance"]["calibration_passed"] is True
    assert record["acceptance"]["no_generator_above_50pct"] is True
    assert set(record["stages"]) == {"generate", "filter", "calibrate", "rank"}
    assert record["gated_stages_not_run"] == ["dock_prepare", "dock", "selectivity", "package"]
    rows = (out / "ranked_candidates.csv").read_text().strip().splitlines()
    assert len(rows) == 31  # header + 30 candidates
    killed = json.loads((out / "kill-ledger.json").read_text())
    assert all({"sequence", "generator", "reason"} <= set(k) for k in killed)
