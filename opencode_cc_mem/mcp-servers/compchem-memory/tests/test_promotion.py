# tests/test_promotion.py
from pathlib import Path
import yaml
from compchem_memory.promotion import eligible_entries, _PROMOTION_MIN_SESSIONS


def _entry(entries_dir, name, title, body, sessions):
    fm = {"title": title, "type": "success_pattern",
          "observed_in_sessions": sessions, "confidence": 0.9}
    p = entries_dir / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def test_eligible_requires_min_distinct_sessions(tmp_path):
    entries = tmp_path / ".magnolia" / "entries"; entries.mkdir(parents=True)
    _entry(entries, "two.md", "two", "b", ["s1", "s2"])              # below floor (N=3)
    ok = _entry(entries, "three.md", "three", "b", ["s1", "s2", "s3"])
    (entries / "INDEX.md").write_text("index")                       # must be skipped

    got = eligible_entries(str(tmp_path / ".magnolia"))
    assert [e["path"] for e in got] == [ok]
    assert _PROMOTION_MIN_SESSIONS == 3


def test_eligible_dedups_sessions_within_entry(tmp_path):
    entries = tmp_path / ".magnolia" / "entries"; entries.mkdir(parents=True)
    _entry(entries, "dup.md", "dup", "b", ["s1", "s1", "s2"])        # only 2 distinct
    assert eligible_entries(str(tmp_path / ".magnolia")) == []


# ---------------------------------------------------------------------------
# Panel tests
# ---------------------------------------------------------------------------
from compchem_memory.promotion import run_panel


def test_panel_survives_on_majority_and_flags_correctness(tmp_path):
    entry = {"id": "e.md", "meta": {"title": "T"}, "body": "use X"}
    votes = [
        {"approve": True,  "correctness_concern": None, "generality_concern": None},
        {"approve": True,  "correctness_concern": None, "generality_concern": "narrow"},
        {"approve": False, "correctness_concern": "wrong for ligands", "generality_concern": None},
    ]
    calls = iter(votes)
    res = run_panel(entry, judge=lambda e, lens_idx: next(calls))
    assert res["approvals"] == 2
    assert res["survives"] is True
    assert res["correctness_flag"] == "wrong for ligands"   # surfaced, not outvoted
    assert len(res["passes"]) == 3


def test_panel_dropped_below_majority(tmp_path):
    entry = {"id": "e.md", "meta": {"title": "T"}, "body": "use X"}
    votes = [
        {"approve": True,  "correctness_concern": None, "generality_concern": None},
        {"approve": False, "correctness_concern": None, "generality_concern": "narrow"},
        {"approve": False, "correctness_concern": None, "generality_concern": "narrow"},
    ]
    calls = iter(votes)
    res = run_panel(entry, judge=lambda e, lens_idx: next(calls))
    assert res["approvals"] == 1
    assert res["survives"] is False


def test_panel_handles_none_vote_as_reject(tmp_path):
    entry = {"id": "e.md", "meta": {"title": "T"}, "body": "use X"}
    res = run_panel(entry, judge=lambda e, lens_idx: None)   # all passes fail
    assert res["approvals"] == 0
    assert res["survives"] is False


# ---------------------------------------------------------------------------
# draft_rule tests
# ---------------------------------------------------------------------------
from compchem_memory.promotion import draft_rule


def test_draft_rule_uses_drafter_and_fills_defaults():
    entry = {"id": "e.md", "meta": {"title": "AIR-free scoring", "tags": ["haddock"]},
             "body": "use AIR-free scores"}
    drafted = draft_rule(entry, drafter=lambda e: {
        "name": "air-free-scoring", "description": "Use AIR-free scores",
        "tags": ["haddock", "scoring"], "body": "Always use AIR-free scores."})
    assert drafted["name"] == "air-free-scoring"
    assert drafted["tags"] == ["haddock", "scoring"]
    assert drafted["body"]


def test_draft_rule_falls_back_on_drafter_failure():
    entry = {"id": "e.md", "meta": {"title": "T", "tags": ["x"]}, "body": "raw body"}
    drafted = draft_rule(entry, drafter=lambda e: None)   # LLM failed
    assert drafted["name"]                                 # derived from title
    assert drafted["body"] == "raw body"                   # falls back to entry body
    assert drafted["tags"] == ["x"]


# ---------------------------------------------------------------------------
# consistency check tests
# ---------------------------------------------------------------------------
from compchem_memory.promotion import check_consistency, _existing_rule_summaries


def test_existing_rule_summaries_reads_name_description(tmp_path):
    rules = tmp_path / "rules"; rules.mkdir()
    (rules / "r1.md").write_text(
        "---\nname: prejob-check\ndescription: validate before submit\n---\n\nbody")
    got = _existing_rule_summaries(str(rules))
    assert {"name": "prejob-check", "description": "validate before submit"} in got


def test_check_consistency_passes_through_checker():
    drafted = {"name": "n", "description": "d", "tags": [], "body": "b"}
    res = check_consistency(drafted, [], checker=lambda d, rules: {
        "status": "duplicate", "related_rule": "prejob-check", "note": "same"})
    assert res["status"] == "duplicate"
    assert res["related_rule"] == "prejob-check"


def test_check_consistency_defaults_ok_on_checker_failure():
    drafted = {"name": "n", "description": "d", "tags": [], "body": "b"}
    res = check_consistency(drafted, [], checker=lambda d, rules: None)
    assert res == {"status": "ok", "related_rule": None, "note": ""}


# ---------------------------------------------------------------------------
# propose_promotions orchestrator tests
# ---------------------------------------------------------------------------
import json as _json
from compchem_memory.promotion import propose_promotions, _entry_key


def _approve_all(entry, lens_idx):
    return {"approve": True, "correctness_concern": None, "generality_concern": None}


def _draft_stub(entry):
    return {"name": entry["meta"]["title"], "description": "d", "tags": [], "body": "B"}


def _ok_checker(drafted, rules):
    return {"status": "ok", "related_rule": None, "note": ""}


def _store_with_eligible(tmp_path):
    store = tmp_path / ".magnolia"; entries = store / "entries"; entries.mkdir(parents=True)
    _entry(entries, "a.md", "Alpha rule", "use alpha", ["s1", "s2", "s3"])
    _entry(entries, "b.md", "too few", "x", ["s1", "s2"])     # ineligible
    return store


def test_propose_writes_survivors_only(tmp_path):
    store = _store_with_eligible(tmp_path)
    res = propose_promotions(str(store), skills_dir=str(tmp_path / "rules"),
                             judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    art = _json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert res["candidates"] == 1
    assert len(art["proposals"]) == 1
    assert art["proposals"][0]["entry_title"] == "Alpha rule"
    assert art["proposals"][0]["drafted_rule"]["name"] == "alpha-rule"
    assert art["applied"] == [] and art["rejected"] == []


def test_propose_carries_rejection_forward(tmp_path):
    store = _store_with_eligible(tmp_path)
    args = dict(skills_dir=str(tmp_path / "rules"), judge=_approve_all,
                drafter=_draft_stub, checker=_ok_checker)
    propose_promotions(str(store), **args)
    # reject index 0 by hand, then regenerate
    art_path = store / "reflex" / "promotion-proposal.json"
    data = _json.loads(art_path.read_text()); data["rejected"] = [0]
    art_path.write_text(_json.dumps(data))
    propose_promotions(str(store), **args)                    # a.md still eligible
    data = _json.loads(art_path.read_text())
    assert data["rejected"] == [0]                            # carried forward by key


# ---------------------------------------------------------------------------
# render_promotions_markdown tests
# ---------------------------------------------------------------------------
from compchem_memory.promotion import render_promotions_markdown


def test_render_lists_pending_with_flags(tmp_path):
    store = _store_with_eligible(tmp_path)
    propose_promotions(str(store), skills_dir=str(tmp_path / "rules"),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    path = render_promotions_markdown(str(store))
    assert path == str(tmp_path / "magnolia-review" / "promotions.md")
    md = Path(path).read_text()
    assert "[0]" in md
    assert "Alpha rule" in md
    assert "action: accept" in md
    assert "~~~~" in md


def test_render_none_when_no_pending(tmp_path):
    store = tmp_path / ".magnolia"; (store / "reflex").mkdir(parents=True)
    (store / "reflex" / "promotion-proposal.json").write_text(
        '{"proposals": [], "applied": [], "rejected": []}')
    assert render_promotions_markdown(str(store)) is None
