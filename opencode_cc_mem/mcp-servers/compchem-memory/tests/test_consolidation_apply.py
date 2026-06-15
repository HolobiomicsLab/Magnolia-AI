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
