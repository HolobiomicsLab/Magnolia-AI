# tests/test_consolidation_safety.py
# Regression suite for the 2026-10-22 subject3 auto-band incident:
# a generic auto-extraction stub and a multi-claim entry were merged into
# unrelated findings at confidence 0.9. These tests use the REAL source files
# (copied from the subject3 store at commit af58796^) as negative fixtures.
import json
import shutil
from pathlib import Path

import yaml

from compchem_memory.consolidation import (
    _load_findings,
    apply_merge,
    auto_apply_band,
    cluster_findings,
    consolidate_project_findings,
    render_review_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures" / "consolidation_bad_merges"

BAD1 = [
    "bad1_canonical_d3_search.md",
    "bad1_source_memory_consolidate.md",
    "bad1_source_memory_search.md",
]
BAD2 = [
    "bad2_canonical_stable_2cluster.md",
    "bad2_source_stub_run.md",
]
GOOD1 = ["good1_backbone_a.md", "good1_backbone_b.md"]
GOOD2 = ["good2_subject2_a.md", "good2_subject2_b.md"]


def _store_from(tmp_path: Path, names: list[str]) -> Path:
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    for name in names:
        shutil.copyfile(FIXTURES / name, staging / name)
    return store


def _cluster(ids: list[str], conf: float = 0.9, why: str = "same claim") -> dict:
    return {"ids": list(ids), "confidence": conf, "rationale": why}


def _artifact(store: Path, proposals: list[dict]) -> None:
    reflex = store / "reflex"
    reflex.mkdir(parents=True, exist_ok=True)
    (reflex / "consolidation-proposal.json").write_text(json.dumps(
        {"proposals": proposals, "applied": [], "rejected": [],
         "rejected_keys": []}, indent=2))


def _proposal(store: Path, names: list[str], conf: float = 0.9) -> dict:
    staging = store / "staging"
    first = staging / names[0]
    return {"confidence": conf, "rationale": "same claim",
            "sources": [str(staging / n) for n in names],
            "canonical": str(first),
            "merged_preview": {"title": "merged", "observation_count": 2,
                               "observed_in_sessions": ["s1", "s2"],
                               "body": "merged body"}}


# ---- pool filters ---------------------------------------------------------

def test_pool_excludes_stub_and_multi_claim_entries(tmp_path):
    store = _store_from(tmp_path, BAD1 + BAD2)
    ids = {e["id"] for e in _load_findings(store / "staging")}

    assert "bad2_source_stub_run.md" not in ids          # generic run record
    assert "bad1_source_memory_consolidate.md" not in ids  # already multi-claim
    assert {"bad1_canonical_d3_search.md", "bad1_source_memory_search.md",
            "bad2_canonical_stable_2cluster.md"} <= ids


def test_observation_sections_stay_in_pool_and_reach_the_judge(tmp_path):
    """`## Observation N` is the standard same-claim append format (both good
    merges in the incident used it). It must stay eligible, but the judge must
    see the observation headings so later evidence is not invisible."""
    staging = tmp_path / "staging"
    staging.mkdir()
    fm = {"title": "multi", "type": "scientific_finding",
          "observed_in_sessions": ["s1"], "tools": [], "tags": []}
    (staging / "multi.md").write_text(
        "---\n" + yaml.dump(fm) + "---\n\nclaim one\n\n"
        "## Observation 2 (2026-09-24)\n\nclaim two\n")

    entries = _load_findings(staging)
    assert [e["id"] for e in entries] == ["multi.md"]

    captured = {}

    def clusterer(payload):
        captured["payload"] = payload
        return []

    cluster_findings(entries, clusterer)
    assert captured["payload"][0]["observations"] == ["## Observation 2 (2026-09-24)"]


def test_merge_produced_body_is_excluded(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    fm = {"title": "merged", "type": "scientific_finding",
          "observed_in_sessions": ["s1"], "tools": [], "tags": []}
    (staging / "merged.md").write_text(
        "---\n" + yaml.dump(fm) + "---\n\nclaim one\n\n"
        "## Corroborating observations (merged)\n\n- (session s2) other claim\n")

    assert _load_findings(staging) == []


# ---- incident fixtures: the exact receipt clusters ------------------------

def test_bad_merge_1_cluster_is_dropped(tmp_path):
    store = _store_from(tmp_path, BAD1)
    seen = {}

    def clusterer(payload):
        seen["ids"] = {p["id"] for p in payload}
        return [_cluster(BAD1)]

    res = consolidate_project_findings(str(store), clusterer=clusterer)

    assert res["clusters"] == 0
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["proposals"] == []
    # The multi-claim member never reached the judge, and nothing was deleted.
    assert "bad1_source_memory_consolidate.md" not in seen["ids"]
    assert (store / "staging" / "bad1_source_memory_consolidate.md").exists()


def test_bad_merge_2_cluster_is_dropped(tmp_path):
    store = _store_from(tmp_path, BAD2)
    seen = {}

    def clusterer(payload):
        seen["ids"] = {p["id"] for p in payload}
        return [_cluster(BAD2)]

    res = consolidate_project_findings(str(store), clusterer=clusterer)

    assert res["clusters"] == 0
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["proposals"] == []
    assert "bad2_source_stub_run.md" not in seen["ids"]
    assert (store / "staging" / "bad2_source_stub_run.md").exists()


def test_recorded_incident_grouping_generates_no_proposal(tmp_path):
    incident = json.loads((FIXTURES / "incident_grouping.json").read_text())
    store = _store_from(tmp_path, BAD1 + BAD2)

    res = consolidate_project_findings(
        str(store),
        clusterer=lambda payload: [incident["bad1_memory_tools"],
                                   incident["bad2_stub_run"]])

    assert res["clusters"] == 0
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["proposals"] == []


def test_good_pairs_still_propose(tmp_path):
    store = _store_from(tmp_path, GOOD1 + GOOD2)

    res = consolidate_project_findings(
        str(store),
        clusterer=lambda payload: [_cluster(GOOD1, 0.95), _cluster(GOOD2, 0.9)])

    assert res["clusters"] == 2
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert len(data["proposals"]) == 2
    assert {Path(s).name for s in data["proposals"][0]["sources"]} == set(GOOD1)
    assert {Path(s).name for s in data["proposals"][1]["sources"]} == set(GOOD2)


# ---- apply-time guard ------------------------------------------------------

def test_apply_merge_refuses_multi_claim_source(tmp_path):
    store = _store_from(tmp_path, ["bad1_source_memory_consolidate.md",
                                   "bad1_source_memory_search.md"])
    paths = [str(store / "staging" / n) for n in
             ("bad1_source_memory_consolidate.md", "bad1_source_memory_search.md")]
    before = {p: Path(p).read_text() for p in paths}

    res = apply_merge(paths)

    assert res["skipped"] is True and res["merged"] is None
    assert {p: Path(p).read_text() for p in paths} == before


def test_apply_merge_refuses_stub_source(tmp_path):
    store = _store_from(tmp_path, BAD2)
    paths = [str(store / "staging" / n) for n in BAD2]
    res = apply_merge(paths)
    assert res["skipped"] is True
    assert all(Path(p).exists() for p in paths)


# ---- review defaults and source detail ------------------------------------

def test_review_defaults_to_reject_and_lists_each_source(tmp_path):
    store = _store_from(tmp_path, ["bad1_canonical_d3_search.md",
                                   "bad1_source_memory_search.md"])
    consolidate_project_findings(
        str(store),
        clusterer=lambda payload: [_cluster(
            ["bad1_canonical_d3_search.md", "bad1_source_memory_search.md"], 0.7)])

    md = Path(render_review_markdown(str(store))).read_text()

    assert "- action: reject" in md
    assert "action: accept" not in md
    assert "`bad1_canonical_d3_search.md` — Multi-tier keyword-split search" in md
    assert "`bad1_source_memory_search.md` — memory_search now supports" in md


# ---- auto-band gate -------------------------------------------------------

def test_auto_band_defers_cross_channel_cluster(tmp_path, monkeypatch):
    store = _store_from(tmp_path, ["bad1_canonical_d3_search.md",
                                   "bad1_source_memory_search.md"])
    consolidate_project_findings(
        str(store),
        clusterer=lambda payload: [_cluster(
            ["bad1_canonical_d3_search.md", "bad1_source_memory_search.md"], 0.9)])
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")

    res = auto_apply_band(str(store))

    assert res["applied"] == 0
    assert res["deferred"][0]["reason"] == "no_shared_tool"
    assert (store / "staging" / "bad1_canonical_d3_search.md").exists()
    assert (store / "staging" / "bad1_source_memory_search.md").exists()
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["applied"] == []
    rows = [json.loads(l) for l in
            (store / "reflex" / "consolidation-auto-log.jsonl").read_text().splitlines()]
    assert rows[0]["applied"] == 0
    assert rows[0]["deferred"][0]["reason"] == "no_shared_tool"


def test_auto_band_defers_legacy_ineligible_proposal(tmp_path, monkeypatch):
    store = _store_from(tmp_path, BAD2)
    _artifact(store, [_proposal(store, BAD2, 0.9)])
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")

    res = auto_apply_band(str(store))

    assert res["applied"] == 0
    assert res["deferred"][0]["reason"] == "ineligible_source"
    assert all((store / "staging" / n).exists() for n in BAD2)


def test_auto_band_applies_shared_channel_cluster(tmp_path, monkeypatch):
    store = _store_from(tmp_path, GOOD1)
    consolidate_project_findings(
        str(store), clusterer=lambda payload: [_cluster(GOOD1, 0.95)])
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")

    res = auto_apply_band(str(store))

    assert res["applied"] == 1
    assert (store / "staging" / GOOD1[0]).exists() != (
        store / "staging" / GOOD1[1]).exists()


# ---- judge payload --------------------------------------------------------

def test_cluster_payload_carries_tools_tags_and_longer_gist():
    entries = [{
        "id": f"e{i}.md", "path": "", "body": "word " * 200,
        "meta": {"title": f"entry {i}", "type": "note",
                 "tools": [f"tool{i}"], "tags": [f"tag{i}"],
                 "opencode_session_id": "s"},
    } for i in range(2)]
    captured = {}

    def clusterer(payload):
        captured["payload"] = payload
        return []

    cluster_findings(entries, clusterer)

    item = captured["payload"][0]
    assert len(item["gist"]) > 200                  # no longer 200-char gist
    assert item["tools"] == ["tool0"]
    assert item["tags"] == ["tag0"]
