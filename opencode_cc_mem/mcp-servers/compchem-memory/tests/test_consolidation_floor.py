# tests/test_consolidation_floor.py
# Volume-tuning generation floor (2026-10-06): clusters the judge scores below
# MAGNOLIA_CONSOLIDATION_FLOOR (default 0.65) never reach the human queue —
# they go to reflex/suppressed-proposals.jsonl so the strictness improvement
# stays measurable. Off at floor=0.
import json

import pytest
import yaml

from compchem_memory.consolidation import (
    _CLUSTER_SYSTEM,
    PROPOSAL_FLOOR_DEFAULT,
    consolidate_project_findings,
)


def _write(staging, name, title, body, ses):
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": ses, "observed_in_sessions": [ses],
          "tags": [], "tools": [], "confidence": 0.6}
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")


def _store_with_pairs(tmp_path, n_pairs=3):
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    for k in range(n_pairs):
        _write(staging, f"a{k}.md", f"pair {k} variant A",
               f"body alpha {k} " + "x" * (k + 1), f"ses_{k}a")
        _write(staging, f"b{k}.md", f"pair {k} variant B",
               f"body beta {k}", f"ses_{k}b")
    return store


def _mixed_clusterer(store):
    staging = store / "staging"
    return lambda payload: [
        {"ids": [f"a0.md", f"b0.md"], "confidence": 0.50, "rationale": "topic only"},
        {"ids": [f"a1.md", f"b1.md"], "confidence": 0.65, "rationale": "same claim"},
        {"ids": [f"a2.md", f"b2.md"], "confidence": 0.90, "rationale": "same claim"},
    ]


def test_default_floor_suppresses_weak_band(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_CONSOLIDATION_FLOOR", raising=False)
    store = _store_with_pairs(tmp_path)

    res = consolidate_project_findings(str(store), clusterer=_mixed_clusterer(store))

    assert res["suppressed"] == 1 and res["clusters"] == 2
    data = json.loads(
        (store / "reflex" / "consolidation-proposal.json").read_text())
    confs = [p["confidence"] for p in data["proposals"]]
    assert confs == [0.65, 0.90]              # 0.50 never became a proposal
    rows = [json.loads(l) for l in
            (store / "reflex" / "suppressed-proposals.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.50
    assert rows[0]["floor"] == pytest.approx(PROPOSAL_FLOOR_DEFAULT)
    assert sorted(rows[0]["sources"]) == ["a0.md", "b0.md"]


def test_floor_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_FLOOR", "0.55")
    store = _store_with_pairs(tmp_path)

    res = consolidate_project_findings(str(store), clusterer=_mixed_clusterer(store))

    assert res["suppressed"] == 1 and res["clusters"] == 2   # 0.50 out, 0.65+0.90 in
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_FLOOR", "0.95")
    res = consolidate_project_findings(str(store), clusterer=_mixed_clusterer(store))
    assert res["suppressed"] == 3 and res["clusters"] == 0
    data = json.loads(
        (store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["proposals"] == []


def test_floor_zero_disables_suppression(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_FLOOR", "0")
    store = _store_with_pairs(tmp_path)

    res = consolidate_project_findings(str(store), clusterer=_mixed_clusterer(store))

    assert res["suppressed"] == 0 and res["clusters"] == 3
    assert not (store / "reflex" / "suppressed-proposals.jsonl").exists()


def test_invalid_floor_value_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_FLOOR", "not-a-float")
    store = _store_with_pairs(tmp_path)

    res = consolidate_project_findings(str(store), clusterer=_mixed_clusterer(store))

    assert res["suppressed"] == 1 and res["clusters"] == 2


def test_prompt_hardened_for_strictness():
    # The judge prompt must carry the two-sided information-loss test, the
    # empty-is-valid permission, and the honest-confidence rule.
    assert "would any information be lost" in _CLUSTER_SYSTEM
    assert "EMPTY" in _CLUSTER_SYSTEM
    assert "never inflate it" in _CLUSTER_SYSTEM
