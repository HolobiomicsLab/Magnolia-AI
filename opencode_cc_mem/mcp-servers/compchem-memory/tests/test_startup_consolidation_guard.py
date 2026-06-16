# tests/test_startup_consolidation_guard.py
"""The consolidation sweep must not regenerate the proposal artifact while a human
review is pending — otherwise a concurrent sweep could reorder proposals so that
memory_apply_consolidation(accept=[i]) merges a cluster the user never confirmed
(the review→apply contract is keyed by positional index)."""
import json
from pathlib import Path
from compchem_memory import startup_scan


def _artifact(store, pending):
    art = store / "reflex" / "consolidation-proposal.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    data = {"proposals": [{"k": 1}], "applied": [], "rejected": []}
    if not pending:
        data["applied"] = [0]          # the lone proposal is handled
    art.write_text(json.dumps(data))


def test_maybe_consolidate_frozen_while_review_pending(tmp_path, monkeypatch):
    store = tmp_path / ".magnolia"; (store / "staging").mkdir(parents=True)
    _artifact(store, pending=True)
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    calls = {"n": 0}
    monkeypatch.setattr("compchem_memory.consolidation.consolidate_project_findings",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    startup_scan._maybe_consolidate(store)
    assert calls["n"] == 0              # frozen — no regeneration while pending


def test_maybe_consolidate_runs_when_nothing_pending(tmp_path, monkeypatch):
    store = tmp_path / ".magnolia"; (store / "staging").mkdir(parents=True)
    _artifact(store, pending=False)     # all proposals handled → not pending
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    monkeypatch.setattr("compchem_memory.consolidation._load_findings",
                        lambda *a, **k: [{}] * 25)   # above the _CONSOLIDATION_MIN_FINDINGS gate
    calls = {"n": 0}
    monkeypatch.setattr("compchem_memory.consolidation.consolidate_project_findings",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    startup_scan._maybe_consolidate(store)
    assert calls["n"] == 1              # not frozen → regenerates normally
