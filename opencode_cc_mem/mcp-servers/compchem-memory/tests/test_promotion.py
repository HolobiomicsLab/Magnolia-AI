# tests/test_promotion.py
from datetime import datetime
from pathlib import Path
import yaml
from compchem_memory.promotion import (
    eligible_entries, _PROMOTION_MIN_SESSIONS, _PROMOTION_PANEL_PASSES,
)


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
    res = propose_promotions(str(store), rules_dir=str(tmp_path / "rules"),
                             judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    art = _json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert res["candidates"] == 1
    assert len(art["proposals"]) == 1
    assert art["proposals"][0]["entry_title"] == "Alpha rule"
    assert art["proposals"][0]["drafted_rule"]["name"] == "alpha-rule"
    assert art["applied"] == [] and art["rejected"] == []


def test_propose_carries_rejection_forward(tmp_path):
    # A user rejection must stay durable across regeneration. Under verdict
    # memoization the entry is NOT re-proposed; the rejection persists in
    # rejected_records keyed by content hash.
    store = _store_with_eligible(tmp_path)
    args = dict(rules_dir=str(tmp_path / "rules"), judge=_approve_all,
                drafter=_draft_stub, checker=_ok_checker)
    propose_promotions(str(store), **args)
    # reject index 0 by hand, then regenerate
    art_path = store / "reflex" / "promotion-proposal.json"
    data = _json.loads(art_path.read_text()); data["rejected"] = [0]
    art_path.write_text(_json.dumps(data))
    propose_promotions(str(store), **args)                    # a.md still eligible
    data = _json.loads(art_path.read_text())
    assert data["proposals"] == []                            # not re-proposed
    (rec,) = data["rejected_records"]
    assert rec["key"] == "a.md"                               # carried forward by key


# ---------------------------------------------------------------------------
# Verdict memoization (skip re-judging unchanged rejections)
# ---------------------------------------------------------------------------
from compchem_memory.promotion import _entry_content_hash
from compchem_memory.reflex_common import parse_frontmatter_file


def _reject_all(entry, lens_idx):
    return {"approve": False, "correctness_concern": None, "generality_concern": "niche"}


def _exploding_judge(entry, lens_idx):
    raise AssertionError("judge must not be called for an unchanged rejected entry")


def _read_artifact(store):
    return _json.loads((store / "reflex" / "promotion-proposal.json").read_text())


def test_propose_records_panel_rejection_with_hash_and_timestamp(tmp_path):
    store = _store_with_eligible(tmp_path)
    res = propose_promotions(str(store), rules_dir=str(tmp_path / "rules"),
                             judge=_reject_all, drafter=_draft_stub, checker=_ok_checker)
    art = _read_artifact(store)
    assert art["proposals"] == [] and res["candidates"] == 0
    entry = parse_frontmatter_file(store / "entries" / "a.md")
    (rec,) = art["rejected_records"]
    assert rec["key"] == "a.md"
    assert rec["entry_hash"] == _entry_content_hash(entry)
    datetime.fromisoformat(rec["last_judged"])                # parses as ISO timestamp


def test_propose_skips_unchanged_rejection_without_calling_judge(tmp_path):
    store = _store_with_eligible(tmp_path)
    args = dict(rules_dir=str(tmp_path / "rules"), drafter=_draft_stub, checker=_ok_checker)
    propose_promotions(str(store), judge=_reject_all, **args)
    prior = _read_artifact(store)["rejected_records"][0]["last_judged"]

    res = propose_promotions(str(store), judge=_exploding_judge, **args)

    assert res["skipped_rejected"] == 1
    art = _read_artifact(store)
    (rec,) = art["rejected_records"]
    assert rec["last_judged"] == prior                        # verdict reused, not refreshed
    assert art["proposals"] == []


def test_propose_rejudges_when_entry_content_changes(tmp_path):
    store = _store_with_eligible(tmp_path)
    args = dict(rules_dir=str(tmp_path / "rules"), drafter=_draft_stub, checker=_ok_checker)
    propose_promotions(str(store), judge=_reject_all, **args)
    prior_hash = _read_artifact(store)["rejected_records"][0]["entry_hash"]
    with open(store / "entries" / "a.md", "a") as fh:         # new observation appended
        fh.write("\n## Observation 2 (2026-09-24)\n\nnew corroborating evidence\n")
    calls = {"n": 0}
    def counting_reject(entry, lens_idx):
        calls["n"] += 1
        return _reject_all(entry, lens_idx)

    res = propose_promotions(str(store), judge=counting_reject, **args)

    assert calls["n"] == _PROMOTION_PANEL_PASSES            # panel re-run on change
    rec = _read_artifact(store)["rejected_records"][0]
    assert rec["entry_hash"] != prior_hash
    assert rec["entry_hash"] == _entry_content_hash(
        parse_frontmatter_file(store / "entries" / "a.md"))


def test_propose_survivor_carries_hash_and_last_judged(tmp_path):
    store = _store_with_eligible(tmp_path)
    propose_promotions(str(store), rules_dir=str(tmp_path / "rules"),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    art = _read_artifact(store)
    (p,) = art["proposals"]
    entry = parse_frontmatter_file(store / "entries" / "a.md")
    assert p["entry_hash"] == _entry_content_hash(entry)
    datetime.fromisoformat(p["last_judged"])


def test_propose_skips_user_rejected_survivor(tmp_path):
    store = _store_with_eligible(tmp_path)
    args = dict(rules_dir=str(tmp_path / "rules"), drafter=_draft_stub, checker=_ok_checker)
    propose_promotions(str(store), judge=_approve_all, **args)
    art_path = store / "reflex" / "promotion-proposal.json"   # user rejects index 0
    data = _json.loads(art_path.read_text()); data["rejected"] = [0]
    art_path.write_text(_json.dumps(data))

    res = propose_promotions(str(store), judge=_exploding_judge, **args)

    assert res["skipped_rejected"] == 1
    art = _read_artifact(store)
    assert art["proposals"] == []
    (rec,) = art["rejected_records"]
    assert rec["key"] == "a.md"
    assert rec["entry_hash"] == data["proposals"][0]["entry_hash"]


def test_propose_legacy_artifact_without_hashes_rejudges_once(tmp_path):
    store = _store_with_eligible(tmp_path)
    (store / "reflex").mkdir(exist_ok=True)                   # pre-fix schema, no hashes
    (store / "reflex" / "promotion-proposal.json").write_text(
        '{"proposals": [], "applied": [], "rejected": []}')
    calls = {"n": 0}
    def counting_approve(entry, lens_idx):
        calls["n"] += 1
        return _approve_all(entry, lens_idx)

    propose_promotions(str(store), rules_dir=str(tmp_path / "rules"),
                       judge=counting_approve, drafter=_draft_stub, checker=_ok_checker)

    assert calls["n"] == _PROMOTION_PANEL_PASSES            # legacy record never matches
    assert _read_artifact(store)["proposals"][0]["entry_title"] == "Alpha rule"


def test_propose_judge_outage_records_nothing_and_is_not_sticky(tmp_path):
    store = _store_with_eligible(tmp_path)
    args = dict(rules_dir=str(tmp_path / "rules"), drafter=_draft_stub, checker=_ok_checker)
    res = propose_promotions(str(store), judge=lambda e, k: None, **args)
    art = _read_artifact(store)
    assert res["candidates"] == 0
    assert art["proposals"] == [] and art["rejected_records"] == []   # outage != rejection

    res = propose_promotions(str(store), judge=_approve_all, **args)
    assert res["candidates"] == 1                             # re-judged next sweep


# ---------------------------------------------------------------------------
# render_promotions_markdown tests
# ---------------------------------------------------------------------------
from compchem_memory.promotion import render_promotions_markdown


def test_render_lists_pending_with_flags(tmp_path):
    store = _store_with_eligible(tmp_path)
    propose_promotions(str(store), rules_dir=str(tmp_path / "rules"),
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


# ---------------------------------------------------------------------------
# apply_promotions tests
# ---------------------------------------------------------------------------
from compchem_memory.promotion import apply_promotions


def test_apply_accept_writes_rule_and_archives_entry(tmp_path):
    store = _store_with_eligible(tmp_path)
    skills = tmp_path / "rules"; skills.mkdir()
    propose_promotions(str(store), rules_dir=str(skills),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    src = _json.loads((store / "reflex" / "promotion-proposal.json").read_text()
                      )["proposals"][0]["source"]

    res = apply_promotions(str(store), str(skills), accept=[0])

    assert res["applied"] == 1
    rule = skills / "alpha-rule.md"
    assert rule.exists()
    txt = rule.read_text()
    assert "last_verified" in txt and "version" in txt
    assert not Path(src).exists()                              # entry archived out
    data = _json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert data["applied"] == [0]


def test_apply_reject_is_durable(tmp_path):
    store = _store_with_eligible(tmp_path); skills = tmp_path / "rules"; skills.mkdir()
    propose_promotions(str(store), rules_dir=str(skills),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    res = apply_promotions(str(store), str(skills), reject=[0])
    assert res["rejected"] == 1
    data = _json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert data["rejected"] == [0]
    assert render_promotions_markdown(str(store)) is None      # not pending


def test_apply_partial_failure_keeps_earlier(tmp_path, monkeypatch):
    from compchem_memory import promotion
    store = tmp_path / ".magnolia"; entries = store / "entries"; entries.mkdir(parents=True)
    _entry(entries, "a.md", "Aaa", "x", ["s1", "s2", "s3"])
    _entry(entries, "b.md", "Bbb", "y", ["s1", "s2", "s3"])
    skills = tmp_path / "rules"; skills.mkdir()
    propose_promotions(str(store), rules_dir=str(skills),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    real = promotion._write_rule
    calls = {"n": 0}
    def flaky(skills_dir, drafted):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(skills_dir, drafted)
    monkeypatch.setattr(promotion, "_write_rule", flaky)

    res = apply_promotions(str(store), str(skills), accept=[0, 1])   # must not raise

    data = _json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert 0 in data["applied"] and 1 not in data["applied"]
    assert 1 in res["failed"]


def test_apply_collision_surfaces_failed_and_preserves_source(tmp_path):
    store = tmp_path / ".magnolia"; entries = store / "entries"; entries.mkdir(parents=True)
    _entry(entries, "a.md", "Same Name", "first body", ["s1", "s2", "s3"])
    _entry(entries, "b.md", "Same Name", "second body", ["s1", "s2", "s3"])  # same slug
    skills = tmp_path / "rules"; skills.mkdir()
    propose_promotions(str(store), rules_dir=str(skills),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)

    res = apply_promotions(str(store), str(skills), accept=[0, 1])

    assert (skills / "same-name.md").exists()
    assert res["applied"] == 1                 # only the first succeeded
    assert res["failed"] == [1]                # collision surfaced, not silent clobber
    data = _json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert data["applied"] == [0]
    src1 = data["proposals"][1]["source"]
    assert Path(src1).exists()                 # losing entry NOT archived — no data loss


def test_apply_archive_failure_not_double_counted(tmp_path, monkeypatch):
    from compchem_memory import promotion
    store = _store_with_eligible(tmp_path); skills = tmp_path / "rules"; skills.mkdir()
    propose_promotions(str(store), rules_dir=str(skills),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    def boom(source, store_dir):
        raise RuntimeError("archive failed")
    monkeypatch.setattr(promotion, "_archive_entry", boom)

    res = apply_promotions(str(store), str(skills), accept=[0])

    assert res["applied"] == 0                  # NOT counted — apply did not complete
    assert res["failed"] == [0]
    data = _json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert data["applied"] == []                # mark not persisted


def test_review_and_apply_tools_end_to_end(tmp_path, monkeypatch):
    from compchem_memory import server
    store = tmp_path / ".magnolia"; entries = store / "entries"; entries.mkdir(parents=True)
    _entry(entries, "a.md", "Alpha rule", "use alpha", ["s1", "s2", "s3"])
    skills = tmp_path / "rules"; skills.mkdir()
    propose_promotions(str(store), rules_dir=str(skills),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)

    pd = str(tmp_path)
    monkeypatch.setattr(server, "PROJECT_DIR", pd)
    monkeypatch.setattr(server, "RULES_DIR", skills)
    review = getattr(server.memory_review_promotions, "fn", server.memory_review_promotions)
    apply = getattr(server.memory_apply_promotions, "fn", server.memory_apply_promotions)

    r = _json.loads(review(project_dir=pd))
    assert r["pending"] == 1 and Path(r["review_file"]).exists()

    out = _json.loads(apply(accept=[0], project_dir=pd))
    assert out["applied"] == 1
    assert (skills / "alpha-rule.md").exists()
    assert not (tmp_path / "magnolia-review" / "promotions.md").exists()  # cleaned
