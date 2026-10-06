# tests/test_auto_confirm.py
# R7 auto-confirm (2026-10-06): staging entries corroborated in >=3 distinct
# sessions promote automatically when MAGNOLIA_AUTO_CONFIRM is set — default
# OFF, nothing changes. Title-clash candidates are HELD (stop-and-flag conflict
# queue), parked entries are immune, canary freeze halts, and the shortlist
# rides the distill-notices queue.
import json

import pytest
import yaml

from compchem_memory.startup_scan import _maybe_auto_confirm
from compchem_memory.tiers.project import ProjectManager


def _write(staging, name, title, obs, sessions, conf=0.6, parked=False):
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": sessions[0], "observed_in_sessions": sessions,
          "observation_count": obs, "tags": [], "tools": [], "confidence": conf}
    if parked:
        fm["parked"] = True
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\nbody\n")


def _project(tmp_path, n_promotable=1, with_conflict=False):
    pd = tmp_path
    store = pd / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    ses = [f"s{i}" for i in range(3)]
    for k in range(n_promotable):
        _write(staging, f"good{k}.md", f"Corroborated finding {k}", 3, ses)
    _write(staging, "two.md", "Only twice seen", 2, ses[:2])
    _write(staging, "parked.md", "Parked corroboration", 3, ses, parked=True)
    if with_conflict:
        entries = store / "entries"
        entries.mkdir(parents=True)
        (entries / "clash.md").write_text("---\n" + yaml.dump({
            "title": "corroborated finding 0", "type": "note",
        }) + "---\n\nproject body\n")
    return pd, store


def _flag(monkeypatch, on=True):
    if on:
        monkeypatch.setenv("MAGNOLIA_AUTO_CONFIRM", "1")
    else:
        monkeypatch.delenv("MAGNOLIA_AUTO_CONFIRM", raising=False)
    # D3 canary must not be frozen in these tests.
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: False)


def test_disabled_by_default(tmp_path, monkeypatch):
    pd, store = _project(tmp_path)
    _flag(monkeypatch, on=False)

    _maybe_auto_confirm(str(pd), store)

    assert (store / "staging" / "good0.md").exists()
    assert not (store / ".magnolia" / "entries").exists() or not list(
        (store / "entries").glob("good0.md"))


def test_promotes_obs3_and_confidence_does_not_block(tmp_path, monkeypatch):
    pd, store = _project(tmp_path)
    _flag(monkeypatch)

    res = ProjectManager(Path_home()).auto_confirm_staging(str(pd))

    assert res["promoted"] == ["good0.md"]
    assert res["conflicts"] == []
    assert (store / "entries" / "good0.md").exists()
    assert not (store / "staging" / "good0.md").exists()


def test_two_sessions_never_promotes(tmp_path, monkeypatch):
    pd, store = _project(tmp_path)
    _flag(monkeypatch)

    res = ProjectManager(Path_home()).auto_confirm_staging(str(pd))

    assert (store / "staging" / "two.md").exists()          # obs=2, 2 sessions
    assert "two.md" not in res["promoted"]


def test_parked_is_immune(tmp_path, monkeypatch):
    pd, store = _project(tmp_path)
    _flag(monkeypatch)

    ProjectManager(Path_home()).auto_confirm_staging(str(pd))

    assert (store / "staging" / "parked.md").exists()


def test_title_conflict_held_not_promoted(tmp_path, monkeypatch):
    pd, store = _project(tmp_path, with_conflict=True)
    _flag(monkeypatch)

    res = ProjectManager(Path_home()).auto_confirm_staging(str(pd))

    assert res["conflicts"] == ["good0.md"]
    assert res["promoted"] == []
    assert (store / "staging" / "good0.md").exists()         # held, not promoted


def test_canary_freeze_halts(tmp_path, monkeypatch):
    pd, store = _project(tmp_path)
    _flag(monkeypatch)
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: True)

    res = ProjectManager(Path_home()).auto_confirm_staging(str(pd))

    assert res == {"promoted": [], "conflicts": []}
    assert (store / "staging" / "good0.md").exists()


def test_sweep_step_pushes_shortlist_notice(tmp_path, monkeypatch):
    from compchem_memory import distill_log
    pd, store = _project(tmp_path, n_promotable=2, with_conflict=True)
    _flag(monkeypatch)

    _maybe_auto_confirm(str(pd), store)

    notices = distill_log.drain_distill_notices(str(pd))
    assert len(notices) == 1
    assert "Auto-confirmed 1" in notices[0]      # good1 promoted
    assert "HELD 1" in notices[0]                # good0 held (title clash)


def Path_home():
    from pathlib import Path
    return Path.home() / ".magnolia"
