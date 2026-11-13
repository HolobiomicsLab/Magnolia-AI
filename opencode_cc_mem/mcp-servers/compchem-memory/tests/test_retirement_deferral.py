# tests/test_retirement_deferral.py
"""One-shot retirement deferral for entries in pending consolidation proposals.

The first time an entry hits the retirement gate while referenced by a pending
proposal, it gets ONE deferral recorded in reflex/retirement-deferrals.json.
It is never renewed: after the deadline the entry retires and the proposal is
dismissed by the stale-proposal cleanup."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from compchem_memory.consolidation import (
    apply_proposals,
    consolidate_project_findings,
    proposal_retirement_status,
    render_review_markdown,
)
from compchem_memory.retirement import retire_eligible

NOW = datetime.now(timezone.utc)


def _entry(staging, name, title, obs=1, created_days_ago=30):
    created = (NOW - timedelta(days=created_days_ago)).isoformat()
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": "s_old", "observed_in_sessions": ["s_old"],
          "observation_count": obs, "tags": [], "tools": [], "confidence": 0.6,
          "created": created}
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\nbody\n")


def _exposure(store, n_opps=12, days=4, start_days_ago=10):
    led = store / "reflex" / "exposure.jsonl"
    led.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n_opps):
        ts = NOW - timedelta(days=(i % days) + start_days_ago, hours=i)
        rows.append({"ts": ts.isoformat(), "kind": "retrieval_opportunity"})
    led.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _store(tmp_path):
    store = tmp_path / ".magnolia"
    (store / "staging").mkdir(parents=True)
    return tmp_path, store


def _fresh_proposal(store, ids=("old_a.md", "old_b.md")):
    consolidate_project_findings(
        str(store),
        clusterer=lambda payload: [{"ids": list(ids), "confidence": 0.9,
                                    "rationale": "same claim"}],
    )
    return store / "reflex" / "consolidation-proposal.json"


def _ready(tmp_path, monkeypatch):
    pd, store = _store(tmp_path)
    st = store / "staging"
    _entry(st, "old_a.md", "Old A")
    _entry(st, "old_b.md", "Old B")
    _exposure(store)
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: False)
    return pd, store, st


def _deferrals(store):
    path = store / "reflex" / "retirement-deferrals.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _expire_deferrals(store):
    path = store / "reflex" / "retirement-deferrals.json"
    records = json.loads(path.read_text())
    past = (NOW - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({k: past for k in records}))


def test_proposed_at_is_stamped(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    artifact = _fresh_proposal(store)
    data = json.loads(artifact.read_text())
    assert "proposed_at" in data["proposals"][0]


def test_first_gate_creates_one_shot_deferral(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)

    res = retire_eligible(str(pd))

    assert res["retired"] == []
    assert sorted(res["deferred"]) == ["old_a.md", "old_b.md"]
    assert res["next_deadline"] is not None
    records = _deferrals(store)
    assert len(records) == 2
    assert all(datetime.fromisoformat(d) > NOW for d in records.values())
    assert (st / "old_a.md").exists() and (st / "old_b.md").exists()


def test_deferral_is_not_renewed_within_grace(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)
    retire_eligible(str(pd))
    before = _deferrals(store)

    res = retire_eligible(str(pd))

    assert res["retired"] == []
    assert _deferrals(store) == before  # same deadlines, not extended


def test_expired_deferral_retires_and_dismisses(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    artifact = _fresh_proposal(store)
    retire_eligible(str(pd))
    _expire_deferrals(store)

    res = retire_eligible(str(pd))

    assert res["retired"] == ["old_a.md"]  # velocity cap 1 for a 2-entry pool
    assert res["deferred"] == []
    assert res["dismissed_proposals"] == [0]
    assert (store / "retired" / "old_a.md").exists()
    data = json.loads(artifact.read_text())
    assert data["dismissed"] == [0]


def test_expired_record_is_not_renewed_when_cap_blocks(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)
    retire_eligible(str(pd))
    _expire_deferrals(store)
    first = retire_eligible(str(pd))
    assert first["retired"] == ["old_a.md"]
    assert first["deferred"] == []  # old_b was NOT re-deferred

    second = retire_eligible(str(pd))

    assert second["retired"] == ["old_b.md"]
    assert second["deferred"] == []
    assert second["dismissed_proposals"] == []  # already dismissed on the first pass


def test_zero_grace_disables_deferral(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)
    monkeypatch.setenv("MAGNOLIA_RETIREMENT_PROPOSAL_GRACE_DAYS", "0")

    res = retire_eligible(str(pd))

    assert res["retired"] == ["old_a.md"]
    assert res["deferred"] == []
    assert res["dismissed_proposals"] == [0]


def test_reject_clears_deferral(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)
    retire_eligible(str(pd))
    apply_proposals(str(store), [], reject=[0])

    res = retire_eligible(str(pd))

    assert res["retired"] == ["old_a.md"]
    assert res["deferred"] == []


def test_notice_warns_about_deadline(tmp_path, monkeypatch):
    from compchem_memory.startup_scan import _surface_pending_consolidation
    from compchem_memory import distill_log
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)
    retire_eligible(str(pd))  # records the deferral deadline

    _surface_pending_consolidation(str(pd), store)

    notices = distill_log.drain_distill_notices(str(pd))
    assert len(notices) == 1
    assert "Review before" in notices[0]
    assert "dismissed" in notices[0]


def test_review_shows_deadline(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)
    retire_eligible(str(pd))

    md = render_review_markdown(str(store))

    text = Path(md).read_text()
    assert "review by:" in text
    assert "sources protected from retirement" in text


def test_status_has_no_deadline_before_first_sweep(tmp_path, monkeypatch):
    pd, store, st = _ready(tmp_path, monkeypatch)
    _fresh_proposal(store)

    status = proposal_retirement_status(str(store))

    assert status["pending"] == 1
    assert status["next_deadline"] is None
    assert status["protected"] == {}
