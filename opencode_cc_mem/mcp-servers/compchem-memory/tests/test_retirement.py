# tests/test_retirement.py
# Retirement v2 (2026-10-06): exposure-rule retirement, dormant behind
# MAGNOLIA_RETIREMENT. Eligibility = obs<3 AND surfaced<=1 session AND
# >=10 retrieval opportunities across >=3 days since creation. Velocity cap
# min(20, 5% of staging). Moves are non-destructive (to <store>/retired/),
# receipted, and noticed.
import json
from datetime import datetime, timedelta, timezone

import yaml

from compchem_memory.retirement import (
    MIN_OPPORTUNITY_DAYS,
    record_opportunity,
    retire_eligible,
    retirement_candidates,
)
from compchem_memory.startup_scan import _maybe_retire

NOW = datetime.now(timezone.utc)


def _entry(staging, name, title, obs=1, created_days_ago=30, parked=False):
    created = (NOW - timedelta(days=created_days_ago)).isoformat()
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": "s_old", "observed_in_sessions": ["s_old"],
          "observation_count": obs, "tags": [], "tools": [], "confidence": 0.6,
          "created": created}
    if parked:
        fm["parked"] = True
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\nbody\n")


def _exposure(store, n_opps=12, days=4, start_days_ago=10):
    """Opportunities spread over `days` distinct days, all after any created."""
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


def test_disabled_by_default(tmp_path, monkeypatch):
    pd, store = _store(tmp_path)
    _entry(store / "staging", "old.md", "Old never-used", obs=1)
    _exposure(store)
    monkeypatch.delenv("MAGNOLIA_RETIREMENT", raising=False)

    _maybe_retire(str(pd), store)

    assert (store / "staging" / "old.md").exists()          # dormant: untouched
    assert not (store / "retired").exists()


def test_eligible_entry_retires_with_receipt(tmp_path, monkeypatch):
    pd, store = _store(tmp_path)
    _entry(store / "staging", "old.md", "Old never-used", obs=1, created_days_ago=30)
    _exposure(store)
    monkeypatch.setenv("MAGNOLIA_RETIREMENT", "1")
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: False)

    res = retire_eligible(str(pd))

    assert res["retired"] == ["old.md"]
    assert not (store / "staging" / "old.md").exists()
    assert (store / "retired" / "old.md").exists()          # non-destructive move
    rows = [json.loads(l) for l in
            (store / "reflex" / "retirement-log.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["entry"] == "old.md"
    assert rows[0]["obs"] == 1 and rows[0]["opportunities"] == 12


def test_hard_floor_obs3_and_surfaced2_never_retire(tmp_path, monkeypatch):
    pd, store = _store(tmp_path)
    st = store / "staging"
    _entry(st, "corroborated.md", "Seen 3x", obs=3, created_days_ago=30)
    _entry(st, "popular.md", "Injected often", obs=1, created_days_ago=30)
    _exposure(store)
    # popular.md surfaced in 2 distinct sessions via the action-retrieval log.
    (store / "action-retrieval.jsonl").write_text(
        json.dumps({"ts": "t", "sessionID": "ses_A", "titles": ["Injected often"]}) + "\n"
        + json.dumps({"ts": "t", "sessionID": "ses_B", "titles": ["Injected often"]}) + "\n")
    monkeypatch.setenv("MAGNOLIA_RETIREMENT", "1")

    res = retire_eligible(str(pd))

    assert res["retired"] == []
    assert res["candidates"] == 0                # both hard floors held
    assert (st / "corroborated.md").exists() and (st / "popular.md").exists()


def test_fair_chance_insufficient_opportunities(tmp_path, monkeypatch):
    pd, store = _store(tmp_path)
    _entry(store / "staging", "young.md", "Not evaluated yet", created_days_ago=1)
    _exposure(store, n_opps=3)                   # below E=10
    monkeypatch.setenv("MAGNOLIA_RETIREMENT", "1")

    res = retire_eligible(str(pd))

    assert res["retired"] == []
    assert (store / "staging" / "young.md").exists()


def test_velocity_cap_oldest_first(tmp_path, monkeypatch):
    pd, store = _store(tmp_path)
    st = store / "staging"
    for k in range(30):
        _entry(st, f"e{k:02d}.md", f"Entry {k}", obs=1, created_days_ago=60 - k)
    _exposure(store)
    monkeypatch.setenv("MAGNOLIA_RETIREMENT", "1")

    res = retire_eligible(str(pd))

    assert res["candidates"] == 30
    assert res["cap"] == 1                        # max(1, int(30 * 0.05)) = 1
    assert len(res["retired"]) == 1
    assert res["retired"] == ["e00.md"]           # oldest first
    assert (store / "retired" / "e00.md").exists()


def test_parked_and_canary_immune(tmp_path, monkeypatch):
    pd, store = _store(tmp_path)
    _entry(store / "staging", "parked.md", "Parked", parked=True)
    _exposure(store)
    monkeypatch.setenv("MAGNOLIA_RETIREMENT", "1")
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: True)

    res = retire_eligible(str(pd))

    assert res["skipped_reason"] == "canary_frozen"
    assert (store / "staging" / "parked.md").exists()


def test_sweep_step_pushes_notice(tmp_path, monkeypatch):
    from compchem_memory import distill_log
    pd, store = _store(tmp_path)
    _entry(store / "staging", "old.md", "Old never-used")
    _exposure(store)
    monkeypatch.setenv("MAGNOLIA_RETIREMENT", "1")
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: False)

    _maybe_retire(str(pd), store)

    notices = distill_log.drain_distill_notices(str(pd))
    assert len(notices) == 1 and "Retired 1" in notices[0]


def test_record_opportunity_appends(tmp_path):
    record_opportunity(str(tmp_path))
    record_opportunity(str(tmp_path))
    led = tmp_path / ".magnolia" / "reflex" / "exposure.jsonl"
    rows = [json.loads(l) for l in led.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["kind"] == "retrieval_opportunity"
