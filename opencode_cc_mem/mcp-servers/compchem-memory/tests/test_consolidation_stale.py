# tests/test_consolidation_stale.py
"""Stale consolidation proposals (sources retired/promoted/merged away) must be
dismissed proactively, not left pending to block regeneration and notices."""
import json
from pathlib import Path

import yaml

from compchem_memory.consolidation import (
    consolidate_project_findings,
    dismiss_stale_proposals,
    render_review_markdown,
)
from compchem_memory import server


def _write(staging, name, title, body, ses):
    fm = {"title": title, "type": "scientific_finding", "opencode_session_id": ses,
          "observed_in_sessions": [ses], "tags": [], "tools": [], "confidence": 0.6}
    p = staging / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def _store_with_proposal(tmp_path, ids=("a.md", "b.md")):
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    paths = {}
    for name, ses in (("a.md", "ses_1"), ("b.md", "ses_2"), ("c.md", "ses_3")):
        paths[name] = _write(staging, name, f"{name} finding", f"body {name} longer text", ses)
    consolidate_project_findings(
        str(store),
        clusterer=lambda payload: [{"ids": list(ids), "confidence": 0.95,
                                    "rationale": "same claim"}],
    )
    return store, paths


def test_live_proposal_is_not_dismissed(tmp_path):
    store, _ = _store_with_proposal(tmp_path)
    assert dismiss_stale_proposals(str(store)) == []
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data.get("dismissed", []) == []


def test_missing_source_is_dismissed_and_keyed(tmp_path):
    store, paths = _store_with_proposal(tmp_path)
    Path(paths["a.md"]).unlink()

    assert dismiss_stale_proposals(str(store)) == [0]

    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["dismissed"] == [0]
    assert sorted(map(sorted, data["rejected_keys"])) == [["a.md", "b.md"]]
    assert render_review_markdown(str(store)) is None
    # Idempotent: nothing new on the second call.
    assert dismiss_stale_proposals(str(store)) == []


def test_one_of_three_sources_missing_dismisses_whole_cluster(tmp_path):
    store, paths = _store_with_proposal(tmp_path, ids=("a.md", "b.md", "c.md"))
    Path(paths["b.md"]).unlink()

    assert dismiss_stale_proposals(str(store)) == [0]
    # No merge: surviving sources are untouched and no merged body was written.
    assert Path(paths["a.md"]).exists() and Path(paths["c.md"]).exists()
    assert not any("Corroborating observations (merged)" in p.read_text()
                   for p in (store / "staging").glob("*.md"))


def test_retirement_dismisses_pending_proposal(tmp_path, monkeypatch):
    import compchem_memory.retirement as rt
    store, paths = _store_with_proposal(tmp_path)
    monkeypatch.setattr(rt, "retirement_candidates", lambda s: [
        {"path": Path(paths["a.md"]), "meta": {"title": "a"}, "obs": 1,
         "surfaced": 0, "opportunities": 12, "days": 4, "created": None},
        {"path": Path(paths["b.md"]), "meta": {"title": "b"}, "obs": 1,
         "surfaced": 0, "opportunities": 12, "days": 4, "created": None},
    ])
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: False)

    res = rt.retire_eligible(str(tmp_path))

    assert res["retired"] == ["a.md"]  # velocity cap 1 for a 3-entry staging pool
    assert res["dismissed_proposals"] == [0]
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["dismissed"] == [0]
    assert (store / "retired" / "a.md").exists()
    labels = [json.loads(l) for l in
              (store / "reflex" / "labels.jsonl").read_text().splitlines()]
    assert any(r["verdict"] == "dismiss_stale" and r["via"] == "retirement"
               for r in labels)


def test_review_tool_dismisses_stale_and_cleans_review(tmp_path, monkeypatch):
    store, paths = _store_with_proposal(tmp_path)
    pd = str(tmp_path)
    monkeypatch.setattr(server, "PROJECT_DIR", pd)
    review = getattr(server.memory_review_consolidation, "fn",
                     server.memory_review_consolidation)
    out = json.loads(review(project_dir=pd))
    assert out["status"] == "review_ready"
    assert (tmp_path / "magnolia-review" / "proposals.md").exists()

    Path(paths["a.md"]).unlink()
    out = json.loads(review(project_dir=pd))

    assert out == {"status": "no_pending_proposals", "pending": 0}
    assert not (tmp_path / "magnolia-review").exists()
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["dismissed"] == [0]


def test_surface_skips_stale_proposal(tmp_path):
    from compchem_memory.startup_scan import _surface_pending_consolidation
    from compchem_memory import distill_log
    store, paths = _store_with_proposal(tmp_path)
    Path(paths["a.md"]).unlink()

    _surface_pending_consolidation(str(tmp_path), store)

    assert distill_log.drain_distill_notices(str(tmp_path)) == []
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["dismissed"] == [0]


def test_stale_proposal_does_not_block_regeneration(tmp_path, monkeypatch):
    from compchem_memory import startup_scan
    import compchem_memory.consolidation as cons
    store, paths = _store_with_proposal(tmp_path)
    Path(paths["a.md"]).unlink()
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    monkeypatch.setattr(startup_scan, "_CONSOLIDATION_MIN_FINDINGS", 0)
    monkeypatch.setattr(cons, "auto_apply_band", lambda *a, **k: {"applied": 0})
    called = []
    monkeypatch.setattr(cons, "consolidate_project_findings",
                        lambda *a, **k: called.append(1) or {"clusters": 0})

    startup_scan._maybe_consolidate(store)

    assert called == [1]
