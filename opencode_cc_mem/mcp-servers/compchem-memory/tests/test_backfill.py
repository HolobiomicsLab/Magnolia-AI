# tests/test_backfill.py
# A2 historical backfill: entries with opencode_session_id but no
# observed_in_sessions get [sid] + observation_count>=1. DRY-RUN by default;
# the real run is a phase-gate decision.
import yaml

from compchem_memory.backfill import backfill_observed_sessions


def _entry(d, name, sid, sessions=None):
    fm = {"title": name, "type": "note", "confidence": 0.6,
          "opencode_session_id": sid}
    if sessions is not None:
        fm["observed_in_sessions"] = sessions
    (d / name).write_text("---\n" + yaml.dump(fm) + "---\n\nbody\n")


def _store(tmp_path):
    store = tmp_path / ".magnolia"
    (store / "staging").mkdir(parents=True)
    (store / "entries").mkdir(parents=True)
    return store


def test_dry_run_reports_without_mutating(tmp_path):
    store = _store(tmp_path)
    _entry(store / "staging", "a.md", "ses_1")
    _entry(store / "entries", "b.md", "ses_2")
    _entry(store / "entries", "has_sessions.md", "ses_3", sessions=["ses_3"])
    _entry(store / "entries", "no_sid.md", "")

    res = backfill_observed_sessions(str(store), dry_run=True)

    assert res["dry_run"] is True and res["count"] == 2
    assert sorted(res["changed"]) == ["entries/b.md", "staging/a.md"]
    text = (store / "staging" / "a.md").read_text()
    assert "observed_in_sessions" not in text       # untouched


def test_real_run_fills_and_keeps_existing(tmp_path):
    store = _store(tmp_path)
    _entry(store / "staging", "a.md", "ses_1")
    _entry(store / "entries", "has_sessions.md", "ses_3", sessions=["ses_9"])

    res = backfill_observed_sessions(str(store), dry_run=False)

    assert res["count"] == 1
    text = (store / "staging" / "a.md").read_text()
    assert "observed_in_sessions:" in text and "ses_1" in text
    kept = (store / "entries" / "has_sessions.md").read_text()
    assert "ses_9" in kept and res["count"] == 1    # existing never overwritten
