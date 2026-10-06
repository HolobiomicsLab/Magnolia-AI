# tests/test_consolidation_seed.py
# Flag-gated judge additions (2026-10-06): MAGNOLIA_CONSOLIDATION_SEED injects
# durably-rejected pairs into the judge prompt (stop re-proposing rejects);
# MAGNOLIA_CONSOLIDATION_SIBLING adds the same-session sibling clause. Evidence
# field: a human verdict's "why" lands in the label row.
import json

import yaml

from compchem_memory.consolidation import (
    _CLUSTER_SYSTEM,
    apply_proposals,
    consolidate_project_findings,
)


def _write(staging, name, title, body, ses):
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": ses, "observed_in_sessions": [ses],
          "tags": [], "tools": [], "confidence": 0.6}
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")


def _store_with_rejected_pair(tmp_path):
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    _write(staging, "a.md", "Alpha finding", "body a", "s1")
    _write(staging, "b.md", "Beta finding", "body b", "s2")
    art = store / "reflex" / "consolidation-proposal.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps({
        "proposals": [], "applied": [], "rejected": [],
        "rejected_keys": [["a.md", "b.md"]]}))
    return store


def _capture_llm(monkeypatch):
    seen = []

    def fake_call_llm_json(system, payload, **kw):
        seen.append(system)
        return {"clusters": []}

    monkeypatch.setattr("compchem_memory.llm.call_llm_json", fake_call_llm_json)
    return seen


def test_seed_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_CONSOLIDATION_SEED", raising=False)
    store = _store_with_rejected_pair(tmp_path)
    seen = _capture_llm(monkeypatch)

    consolidate_project_findings(str(store))

    assert seen and all("Never group" not in s for s in seen)


def test_seed_on_injects_rejected_pairs(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_SEED", "1")
    store = _store_with_rejected_pair(tmp_path)
    seen = _capture_llm(monkeypatch)

    consolidate_project_findings(str(store))

    assert seen and all("Never group" in s for s in seen)
    assert "'Alpha finding' with 'Beta finding'" in seen[0]


def test_sibling_clause_flag(tmp_path, monkeypatch):
    assert "siblings" not in _CLUSTER_SYSTEM          # base prompt untouched
    monkeypatch.delenv("MAGNOLIA_CONSOLIDATION_SIBLING", raising=False)
    store = _store_with_rejected_pair(tmp_path)
    seen = _capture_llm(monkeypatch)
    consolidate_project_findings(str(store))
    assert seen and all("siblings" not in s for s in seen)

    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_SIBLING", "1")
    seen.clear()
    consolidate_project_findings(str(store))
    assert seen and all("are siblings" in s for s in seen)


def test_seeded_pair_still_auto_rejected_at_generation(tmp_path, monkeypatch):
    """Seeding never bypasses the durable rejection: a re-proposed rejected pair
    is auto-rejected by content key regardless of the judge's answer."""
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_SEED", "1")
    store = _store_with_rejected_pair(tmp_path)

    res = consolidate_project_findings(
        str(store),
        clusterer=lambda p: [{"ids": ["a.md", "b.md"], "confidence": 0.9,
                              "rationale": "same claim"}])

    data = json.loads(
        (store / "reflex" / "consolidation-proposal.json").read_text())
    assert res["clusters"] == 1
    assert data["rejected"] == [0]                    # carried forward by key
    assert not data["applied"]


def test_evidence_field_lands_in_label(tmp_path):
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    _write(staging, "a.md", "A finding", "longer body", "s1")
    _write(staging, "b.md", "B finding", "short", "s2")
    consolidate_project_findings(
        str(store),
        clusterer=lambda p: [{"ids": ["a.md", "b.md"], "confidence": 0.9,
                              "rationale": "same"}])

    apply_proposals(str(store), [], reject=[0],
                    evidence="user: same topic only, different metrics")

    (r,) = [json.loads(l) for l in
            (store / "reflex" / "labels.jsonl").read_text().splitlines()]
    assert r["evidence"] == "user: same topic only, different metrics"
