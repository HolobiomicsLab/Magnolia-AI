# tests/test_promotion_destinations.py
"""Destination assignment: a lesson that mentions cluster-specific facts must
never be drafted into shared files. The fact patterns come from the user's
LOCAL cluster skills (frontmatter `cluster_facts:`), so the committed repo
stays free of cluster facts."""
import json
from pathlib import Path

import yaml
from compchem_memory.promotion import (
    apply_promotions,
    collect_cluster_fact_patterns,
    destination_for,
    load_destinations,
    propose_promotions,
    render_promotions_markdown,
    DESTINATION_CLUSTER_FILE,
    DESTINATION_SHARED_RULES,
)


def _entry(entries_dir, name, title, body, sessions=("s1", "s2", "s3")):
    fm = {"title": title, "type": "success_pattern",
          "observed_in_sessions": list(sessions), "confidence": 0.9}
    p = entries_dir / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def _approve_all(entry, lens_idx):
    return {"approve": True, "correctness_concern": None, "generality_concern": None}


def _draft_stub(entry):
    return {"name": entry["meta"]["title"], "description": "d", "tags": [], "body": "B"}


def _ok_checker(drafted, rules):
    return {"status": "ok", "related_rule": None, "note": ""}


def _layout(tmp_path):
    """rules/ + a destinations yaml next to it + a local cluster skill whose
    frontmatter declares cluster_facts."""
    rules = tmp_path / "rules"
    rules.mkdir()
    cluster_home = tmp_path / "home-skills"
    skill = cluster_home / "hpc-fakelab" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: hpc-fakelab\ndescription: x\n"
        "cluster_facts:\n  - fakelab\n  - --account[= ]chem42\n---\n\nbody\n")
    (tmp_path / "magnolia-destinations.yaml").write_text(yaml.dump({
        "shared_rules": "rules",
        "cluster_skills": str(cluster_home),
        "cluster_skill_prefix": "hpc-",
    }))
    return rules


def _store(tmp_path, body):
    store = tmp_path / ".magnolia"
    entries = store / "entries"
    entries.mkdir(parents=True)
    _entry(entries, "a.md", "Lesson", body)
    return store


def _propose(tmp_path, body):
    rules = _layout(tmp_path)
    store = _store(tmp_path, body)
    propose_promotions(str(store), rules_dir=str(rules),
                       judge=_approve_all, drafter=_draft_stub, checker=_ok_checker)
    art = json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    return rules, store, art


# ---------------------------------------------------------------------------
# destinations loading + pattern collection
# ---------------------------------------------------------------------------

def test_destinations_file_overrides_defaults(tmp_path):
    rules = _layout(tmp_path)
    got = load_destinations(str(rules))
    assert got["cluster_skills"] == str(tmp_path / "home-skills")
    assert got["shared_rules"] == "rules"
    assert got["cluster_skill_prefix"] == "hpc-"


def test_destinations_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    rules = tmp_path / "rules"
    rules.mkdir()
    got = load_destinations(str(rules))
    assert got["cluster_skills"] == "~/.config/opencode/skills"  # verbatim default
    assert collect_cluster_fact_patterns(got) == []              # nothing there


def test_collect_patterns_from_local_cluster_skill(tmp_path):
    rules = _layout(tmp_path)
    pats = collect_cluster_fact_patterns(load_destinations(str(rules)))
    assert any(p.search("run on fakelab") for p in pats)
    assert any(p.search("--account chem42") for p in pats)
    assert not any(p.search("ordinary gromacs note") for p in pats)


def test_destination_for_matches_title_and_body():
    import re
    pats = [re.compile("fakelab", re.IGNORECASE)]
    hit = {"meta": {"title": "fakelab vpn drops"}, "body": "b"}
    miss = {"meta": {"title": "gromacs"}, "body": "box dodecahedron"}
    assert destination_for(hit, pats) == DESTINATION_CLUSTER_FILE
    assert destination_for(miss, pats) == DESTINATION_SHARED_RULES


# ---------------------------------------------------------------------------
# propose: destination recorded on the proposal
# ---------------------------------------------------------------------------

def test_propose_marks_cluster_file_when_fact_mentioned(tmp_path):
    rules, store, art = _propose(
        tmp_path, "the fakelab cluster rejects --account chem42 without qos")
    assert art["proposals"][0]["destination"] == DESTINATION_CLUSTER_FILE


def test_propose_shared_rules_when_no_cluster_fact(tmp_path):
    rules, store, art = _propose(
        tmp_path, "gromacs: prefer dodecahedron boxes for globular proteins")
    assert art["proposals"][0]["destination"] == DESTINATION_SHARED_RULES


# ---------------------------------------------------------------------------
# review + apply semantics
# ---------------------------------------------------------------------------

def test_render_shows_cluster_file_destination(tmp_path):
    rules, store, art = _propose(tmp_path, "fakelab vpn drops after 30 min")
    md = Path(render_promotions_markdown(str(store))).read_text()
    assert "your cluster file" in md
    assert "never" in md  # "...never written into shared files"


def test_apply_accept_cluster_file_writes_nothing_and_keeps_entry(tmp_path):
    rules, store, art = _propose(tmp_path, "fakelab vpn drops after 30 min")
    src = art["proposals"][0]["source"]
    res = apply_promotions(str(store), str(rules), accept=[0])
    assert res["applied"] == 0
    assert res["deferred_to_cluster_file"] == 1
    assert list(rules.glob("*.md")) == []      # nothing written into shared rules
    assert Path(src).exists()                   # source entry preserved
    data = json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert data["applied"] == [0]               # marked handled


def test_apply_promote_raw_refused_for_cluster_file(tmp_path):
    rules, store, art = _propose(tmp_path, "fakelab vpn drops after 30 min")
    res = apply_promotions(str(store), str(rules), promote_raw=[0])
    assert res["promoted_raw"] == 0
    assert res["failed"] == [0]
    assert list(rules.glob("*.md")) == []
    data = json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    assert data["applied"] == []                # not marked handled


def test_apply_shared_rules_destination_unchanged(tmp_path):
    rules, store, art = _propose(tmp_path, "gromacs: prefer dodecahedron boxes")
    src = art["proposals"][0]["source"]
    res = apply_promotions(str(store), str(rules), accept=[0])
    assert res["applied"] == 1
    assert res["deferred_to_cluster_file"] == 0
    assert (rules / "lesson.md").exists()
    assert not Path(src).exists()                 # archived as before
