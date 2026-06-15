# tests/test_consolidation_apply.py
from pathlib import Path
import yaml
from compchem_memory.consolidation import apply_merge


def _write(staging, name, title, body, ses):
    fm = {"title": title, "type": "scientific_finding", "opencode_session_id": ses,
          "observed_in_sessions": [ses], "tags": [], "tools": [], "confidence": 0.6}
    p = staging / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def test_apply_merge_overwrites_canonical_and_removes_others(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    a = _write(staging, "a.md", "short", "tiny", "ses_1")
    b = _write(staging, "b.md", "long", "the much longer canonical body text here", "ses_2")
    c = _write(staging, "c.md", "mid", "middle length body", "ses_2")

    res = apply_merge([a, b, c])

    assert res["skipped"] is False
    assert res["merged"] == b
    assert Path(b).exists()
    merged_text = Path(b).read_text()
    meta = yaml.safe_load(merged_text.split("---")[1])
    assert meta["observation_count"] == 2
    assert "tiny" in merged_text
    assert not Path(a).exists()
    assert not Path(c).exists()
    assert set(res["removed"]) == {a, c}


def test_apply_merge_skips_when_fewer_than_two_sources_survive(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    a = _write(staging, "a.md", "x", "body", "ses_1")
    res = apply_merge([a, str(staging / "b.md")])
    assert res["skipped"] is True
    assert res["merged"] is None
    assert Path(a).exists()


import json
from compchem_memory.consolidation import apply_proposals, consolidate_project_findings


def _store_with_proposal(tmp_path):
    store = tmp_path / ".magnolia"
    staging = store / "staging"; staging.mkdir(parents=True)
    a = _write(staging, "a.md", "N-term ALA wins", "canonical longer body", "ses_1")
    b = _write(staging, "b.md", "ALA beats C-term", "short", "ses_2")
    c = _write(staging, "c.md", "lone finding", "unrelated", "ses_3")
    consolidate_project_findings(
        str(store),
        clusterer=lambda payload: [{"ids": ["a.md", "b.md"], "confidence": 0.95, "rationale": "same"}],
    )
    return store, a, b, c


def test_apply_proposals_applies_accepted_index_and_marks_it(tmp_path):
    store, a, b, c = _store_with_proposal(tmp_path)

    res = apply_proposals(str(store), [0])

    assert res["applied"] == 1
    surviving = [p for p in (a, b) if Path(p).exists()]
    assert len(surviving) == 1
    assert Path(c).exists()               # c untouched (never in a cluster)
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["applied"] == [0]


def test_apply_proposals_ignores_unknown_or_already_applied(tmp_path):
    store, *_ = _store_with_proposal(tmp_path)
    apply_proposals(str(store), [0])
    res = apply_proposals(str(store), [0, 99])      # re-apply 0 + out-of-range
    assert res["applied"] == 0


from compchem_memory.consolidation import render_review_markdown


def test_render_review_markdown_to_visible_dir(tmp_path):
    store, *_ = _store_with_proposal(tmp_path)
    path = render_review_markdown(str(store))
    assert path == str(tmp_path / "magnolia-review" / "proposals.md")
    md = Path(path).read_text()
    assert "[0]" in md
    assert "action: accept" in md
    assert "N-term ALA wins" in md or "ALA beats C-term" in md


def test_render_review_markdown_none_when_no_unapplied(tmp_path):
    store, *_ = _store_with_proposal(tmp_path)
    apply_proposals(str(store), [0])
    assert render_review_markdown(str(store)) is None


def test_review_and_apply_tools(tmp_path, monkeypatch):
    from compchem_memory import server

    store, a, b, c = _store_with_proposal(tmp_path)
    pd = str(tmp_path)
    monkeypatch.setattr(server, "PROJECT_DIR", pd)

    review = getattr(server.memory_review_consolidation, "fn", server.memory_review_consolidation)
    apply = getattr(server.memory_apply_consolidation, "fn", server.memory_apply_consolidation)

    r = json.loads(review(project_dir=pd))
    assert r["pending"] == 1
    assert Path(r["review_file"]).exists()

    out = json.loads(apply(accept=[0], project_dir=pd))
    assert out["applied"] == 1
    assert sum(Path(p).exists() for p in (a, b)) == 1
    assert Path(c).exists()
    assert not (tmp_path / "magnolia-review").exists()


# ---- Increment B hardening: reject-state (#1) + partial-failure (#2) + render (#3) ----

def test_reject_excludes_from_pending_and_allows_cleanup(tmp_path):
    store, *_ = _store_with_proposal(tmp_path)            # 1 proposal (index 0)
    res = apply_proposals(str(store), [], reject=[0])
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["rejected"] == [0]
    assert render_review_markdown(str(store)) is None    # rejected -> not pending


def test_partial_failure_keeps_earlier_applied(tmp_path, monkeypatch):
    from compchem_memory import consolidation
    store = tmp_path / ".magnolia"; staging = store / "staging"; staging.mkdir(parents=True)
    _write(staging, "a.md", "A1", "longer body a", "s1"); _write(staging, "b.md", "A2", "b", "s2")
    _write(staging, "d.md", "D1", "longer body d", "s1"); _write(staging, "e.md", "D2", "e", "s2")
    consolidate_project_findings(str(store), clusterer=lambda p: [
        {"ids": ["a.md", "b.md"], "confidence": 0.9, "rationale": "x"},
        {"ids": ["d.md", "e.md"], "confidence": 0.9, "rationale": "y"}])
    real = consolidation.apply_merge
    calls = {"n": 0}
    def flaky(paths):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(paths)
    monkeypatch.setattr(consolidation, "apply_merge", flaky)

    res = consolidation.apply_proposals(str(store), [0, 1])   # must not raise

    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert 0 in data["applied"]          # first succeeded + persisted
    assert 1 not in data["applied"]      # second failed
    assert 1 in res["failed"]


def test_apply_tool_cleans_review_dir_after_reject(tmp_path, monkeypatch):
    from compchem_memory import server
    store, *_ = _store_with_proposal(tmp_path)
    pd = str(tmp_path); monkeypatch.setattr(server, "PROJECT_DIR", pd)
    review = getattr(server.memory_review_consolidation, "fn", server.memory_review_consolidation)
    apply = getattr(server.memory_apply_consolidation, "fn", server.memory_apply_consolidation)
    review(project_dir=pd)
    assert (tmp_path / "magnolia-review").exists()
    json.loads(apply(accept=[], reject=[0], project_dir=pd))   # reject the only proposal
    assert not (tmp_path / "magnolia-review").exists()         # all handled -> cleaned


def test_render_uses_tilde_fence_and_marks_truncation(tmp_path):
    store = tmp_path / ".magnolia"; staging = store / "staging"; staging.mkdir(parents=True)
    body = "```\nsome code\n```\n" + "x" * 2000
    _write(staging, "a.md", "Long finding", body, "s1")
    _write(staging, "b.md", "Long finding alt", "short", "s2")
    consolidate_project_findings(str(store), clusterer=lambda p: [
        {"ids": ["a.md", "b.md"], "confidence": 0.9, "rationale": "x"}])
    md = Path(render_review_markdown(str(store))).read_text()
    assert "~~~~" in md                  # tilde fence won't collide with ``` in the body
    assert "(truncated)" in md


def test_reject_survives_consolidation_regeneration(tmp_path):
    """A rejected cluster that re-clusters on the next sweep must stay dismissed —
    reject identity is content-based, carried forward across artifact regeneration."""
    store = tmp_path / ".magnolia"; staging = store / "staging"; staging.mkdir(parents=True)
    _write(staging, "x.md", "X finding", "body x longer text", "s1")
    _write(staging, "y.md", "Y finding", "body y", "s2")
    clusterer = lambda p: [{"ids": ["x.md", "y.md"], "confidence": 0.9, "rationale": "same"}]

    consolidate_project_findings(str(store), clusterer=clusterer)
    apply_proposals(str(store), [], reject=[0])          # reject (non-destructive: x,y remain)
    consolidate_project_findings(str(store), clusterer=clusterer)   # next sweep regenerates

    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    assert data["rejected"] == [0]                       # carried forward by content key
    assert render_review_markdown(str(store)) is None    # not pending -> no re-nag
