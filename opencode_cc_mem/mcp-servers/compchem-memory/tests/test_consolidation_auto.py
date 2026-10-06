# tests/test_consolidation_auto.py
# Auto-band consolidation gate (2026-10-05): MAGNOLIA_CONSOLIDATION_AUTO=1
# applies pending proposals with confidence >= 0.8 without human review,
# capped per sweep, with a JSONL receipt. Off by default.
import json
from pathlib import Path

import pytest
import yaml

from compchem_memory.consolidation import (
    apply_proposals,
    auto_apply_band,
    consolidate_project_findings,
)


def _write(staging: Path, name: str, title: str, body: str, ses: str) -> str:
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": ses, "observed_in_sessions": [ses],
          "tags": [], "tools": [], "confidence": 0.6}
    p = staging / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def _store_with_pairs(tmp_path: Path, n_pairs: int = 2) -> Path:
    """A store whose staging holds n_pairs of same-claim entries (each pair
    shares a session so observation counting stays simple)."""
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    for k in range(n_pairs):
        _write(staging, f"a{k}.md", f"pair {k} variant A", f"body alpha {k} " + "x" * (k + 1), f"ses_{k}a")
        _write(staging, f"b{k}.md", f"pair {k} variant B", f"body beta {k}", f"ses_{k}b")
    return store


def _artifact(store: Path, proposals: list[dict], rejected: list[int] | None = None) -> None:
    reflex = store / "reflex"
    reflex.mkdir(parents=True, exist_ok=True)
    (reflex / "consolidation-proposal.json").write_text(json.dumps(
        {"proposals": proposals, "applied": [], "rejected": rejected or [],
         "rejected_keys": []}, indent=2))


def _pair_proposal(store: Path, k: int, conf: float) -> dict:
    staging = store / "staging"
    return {"confidence": conf, "rationale": "same claim",
            "sources": [str(staging / f"a{k}.md"), str(staging / f"b{k}.md")],
            "canonical": str(staging / f"a{k}.md"),
            "merged_preview": {"title": f"pair {k}", "observation_count": 2,
                               "observed_in_sessions": [f"ses_{k}a", f"ses_{k}b"],
                               "body": "merged body"}}


def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_CONSOLIDATION_AUTO", raising=False)
    store = _store_with_pairs(tmp_path)
    _artifact(store, [_pair_proposal(store, 0, 0.9)])

    res = auto_apply_band(str(store))

    assert res["applied"] == 0 and res["skipped_reason"] == "disabled"
    # Nothing merged: both sources still on disk, nothing marked applied.
    assert (store / "staging" / "a0.md").exists()
    assert (store / "staging" / "b0.md").exists()
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["applied"] == []


def test_applies_high_band_and_leaves_low_band(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")
    store = _store_with_pairs(tmp_path)
    _artifact(store, [_pair_proposal(store, 0, 0.6),   # below band
                      _pair_proposal(store, 1, 0.9)])  # in band

    res = auto_apply_band(str(store))

    assert res["applied"] == 1
    # Pair 1 (in band) merged on disk: canonical (longest body = a1) survives,
    # sibling removed.
    assert (store / "staging" / "a1.md").exists()
    assert not (store / "staging" / "b1.md").exists()
    # Pair 0 (below band) untouched, still pending for human review.
    assert (store / "staging" / "a0.md").exists()
    assert (store / "staging" / "b0.md").exists()
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["applied"] == [1]
    assert data["rejected"] == []
    # Receipt row recorded with the band metadata.
    rows = [json.loads(l) for l in
            (store / "reflex" / "consolidation-auto-log.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["auto"] is True
    assert rows[0]["requested"] == [1]
    assert rows[0]["candidates"][0]["confidence"] == 0.9


def test_velocity_cap_limits_each_sweep(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO_MAX", "1")
    store = _store_with_pairs(tmp_path)
    _artifact(store, [_pair_proposal(store, 0, 0.85),
                      _pair_proposal(store, 1, 0.9)])  # highest first

    res = auto_apply_band(str(store))

    assert res["applied"] == 1
    assert (store / "staging" / "b1.md").exists() is False   # 0.9 applied (a1 canonical)
    assert (store / "staging" / "a0.md").exists()            # 0.85 waits
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["applied"] == [1]


def test_below_band_never_auto_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")
    store = _store_with_pairs(tmp_path)
    _artifact(store, [_pair_proposal(store, 0, 0.79)])

    res = auto_apply_band(str(store))

    assert res["skipped_reason"] == "none_in_band"
    assert (store / "staging" / "a0.md").exists()
    assert not (store / "reflex" / "consolidation-auto-log.jsonl").exists()


def test_rejected_pair_never_auto_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")
    store = _store_with_pairs(tmp_path)
    _artifact(store, [_pair_proposal(store, 0, 0.95)], rejected=[0])

    res = auto_apply_band(str(store))

    # Index 0 is durably rejected -> not pending -> band finds nothing.
    assert res["skipped_reason"] == "none_in_band"
    assert (store / "staging" / "a0.md").exists()


def test_end_to_end_via_sweep(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")
    store = _store_with_pairs(tmp_path, n_pairs=1)
    a = store / "staging" / "a0.md"
    b = store / "staging" / "b0.md"

    def clusterer(payload):
        ids = [p["id"] for p in payload]
        return [{"ids": ids, "confidence": 0.9, "rationale": "same claim"}]

    res = consolidate_project_findings(str(store), clusterer=clusterer)

    assert res["clusters"] == 1
    assert res["auto_applied"] == 1
    assert a.exists() and not b.exists()  # canonical (longest body = a0) survives
    # The auto-action is surfaced, never silent: a notice rides the queue.
    from compchem_memory import distill_log
    notices = distill_log.drain_distill_notices(str(store.parent))
    assert len(notices) == 1 and "Auto-merged 1" in notices[0]
    rows = [json.loads(l) for l in
            (store / "reflex" / "consolidation-auto-log.jsonl").read_text().splitlines()]
    assert rows[0]["applied"] == 1


def test_apply_proposals_still_works_standalone(tmp_path):
    # Guard: the human path is unchanged by the auto-band additions.
    store = _store_with_pairs(tmp_path)
    _artifact(store, [_pair_proposal(store, 0, 0.9)])

    res = apply_proposals(str(store), accepted=[0])

    assert res["applied"] == 1
    assert (store / "staging" / "b0.md").exists() is False  # a0 is canonical
    assert (store / "staging" / "a0.md").exists()
