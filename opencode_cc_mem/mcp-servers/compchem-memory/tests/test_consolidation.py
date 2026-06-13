from compchem_memory.consolidation import merge_entries


def _e(path, title, body, sessions=None, ses=None, tags=None, conf=0.5):
    meta = {"title": title, "type": "scientific_finding", "confidence": conf,
            "tags": tags or []}
    if sessions is not None:
        meta["observed_in_sessions"] = sessions
    if ses is not None:
        meta["opencode_session_id"] = ses
    return {"id": path, "path": path, "meta": meta, "body": body}


def test_merge_unions_distinct_sessions_not_copy_count():
    entries = [
        _e("a.md", "N-term ALA extension wins", "long canonical body here .....", ses="ses_1"),
        _e("b.md", "ALA extension outperforms C-term", "body two", ses="ses_1"),
        _e("c.md", "N-terminal alanine beats C-terminal", "body three", ses="ses_2"),
    ]
    merged = merge_entries(entries)
    assert sorted(merged["meta"]["observed_in_sessions"]) == ["ses_1", "ses_2"]
    assert merged["meta"]["observation_count"] == 2


def test_merge_picks_longest_body_as_canonical_and_keeps_provenance():
    entries = [
        _e("a.md", "short", "tiny", ses="ses_1"),
        _e("b.md", "long one", "this is the much longer canonical body text", ses="ses_2"),
    ]
    merged = merge_entries(entries)
    assert "much longer canonical" in merged["body"]
    assert "tiny" in merged["body"]
    assert set(merged["sources"]) == {"a.md", "b.md"}
    assert merged["canonical"] == "b.md"


def test_merge_unions_tags_and_takes_max_confidence():
    entries = [
        _e("a.md", "x", "body a", ses="ses_1", tags=["haddock3"], conf=0.6),
        _e("b.md", "y", "body b", ses="ses_2", tags=["hsc70"], conf=0.8),
    ]
    merged = merge_entries(entries)
    assert set(merged["meta"]["tags"]) == {"haddock3", "hsc70"}
    assert merged["meta"]["confidence"] == 0.8


import json
from pathlib import Path
import yaml
from compchem_memory.consolidation import cluster_findings, _load_findings


def _write(staging, name, title, body, typ="scientific_finding", ses="ses_x"):
    fm = {"title": title, "type": typ, "opencode_session_id": ses,
          "observed_in_sessions": [ses], "tags": [], "tools": [], "confidence": 0.6}
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")


def test_load_findings_filters_to_nl_types(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    _write(staging, "a.md", "finding A", "body", typ="scientific_finding")
    _write(staging, "b.md", "an error", "body", typ="error_resolution")  # deterministic -> excluded
    entries = _load_findings(staging)
    titles = {e["meta"]["title"] for e in entries}
    assert "finding A" in titles
    assert "an error" not in titles
    assert all(e["id"] == e["path"].split("/")[-1] for e in entries)  # id is filename


def test_cluster_findings_resolves_ids_and_drops_singletons(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    _write(staging, "a.md", "N-term ALA wins", "b1", ses="ses_1")
    _write(staging, "b.md", "ALA beats C-term", "b2", ses="ses_2")
    _write(staging, "c.md", "unrelated finding", "b3", ses="ses_3")
    entries = _load_findings(staging)

    def fake_clusterer(payload):
        return [{"ids": ["a.md", "b.md"], "confidence": 0.95, "rationale": "same claim"},
                {"ids": ["c.md"], "confidence": 0.9, "rationale": "alone"}]

    clusters = cluster_findings(entries, fake_clusterer)
    assert len(clusters) == 1                      # singleton dropped
    assert clusters[0]["confidence"] == 0.95
    assert {m["id"] for m in clusters[0]["members"]} == {"a.md", "b.md"}


from compchem_memory.consolidation import consolidate_project_findings


def test_consolidate_writes_proposal_and_mutates_nothing(tmp_path):
    store = tmp_path / ".magnolia"
    staging = store / "staging"; staging.mkdir(parents=True)
    _write(staging, "a.md", "N-term ALA wins", "canonical body longer text", ses="ses_1")
    _write(staging, "b.md", "ALA beats C-term", "shorter", ses="ses_2")
    before = {p.name: p.read_text() for p in staging.glob("*.md")}

    def fake_clusterer(payload):
        return [{"ids": ["a.md", "b.md"], "confidence": 0.95, "rationale": "same claim"}]

    result = consolidate_project_findings(str(store), clusterer=fake_clusterer)

    art = store / "reflex" / "consolidation-proposal.json"
    assert art.exists()
    data = json.loads(art.read_text())
    assert len(data["proposals"]) == 1
    p = data["proposals"][0]
    assert p["confidence"] == 0.95
    assert set(p["sources"]) == {str(staging / "a.md"), str(staging / "b.md")}
    assert p["merged_preview"]["observation_count"] == 2
    after = {p.name: p.read_text() for p in staging.glob("*.md")}
    assert after == before                       # proposal-only: nothing changed
    assert result["clusters"] == 1
    assert result["artifact"] == str(art)        # spec'd return contract
    assert data["applied"] == []                 # nothing applied in Increment A


def test_consolidate_no_clusters_writes_empty_proposal(tmp_path):
    store = tmp_path / ".magnolia"
    staging = store / "staging"; staging.mkdir(parents=True)
    _write(staging, "a.md", "lone finding", "body", ses="ses_1")
    result = consolidate_project_findings(str(store), clusterer=lambda payload: [])
    assert result["clusters"] == 0
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["proposals"] == []


def test_default_clusterer_resilient_to_bad_llm_output(monkeypatch):
    from compchem_memory import consolidation, llm
    payload = [{"id": "a.md", "title": "x", "gist": "g"}]
    for bad in (None, "not a dict", ["a", "list"], {"clusters": None}, {"no_clusters": 1}):
        monkeypatch.setattr(llm, "call_llm_json", lambda *a, **k: bad)
        assert consolidation._default_clusterer(payload) == []
