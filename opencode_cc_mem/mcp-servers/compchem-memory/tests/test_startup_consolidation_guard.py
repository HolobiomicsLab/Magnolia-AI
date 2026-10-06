# tests/test_startup_consolidation_guard.py
"""The consolidation sweep must not regenerate the proposal artifact while a human
review is pending — otherwise a concurrent sweep could reorder proposals so that
memory_apply_consolidation(accept=[i]) merges a cluster the user never confirmed
(the review→apply contract is keyed by positional index)."""
import json
from pathlib import Path
import yaml

from compchem_memory import startup_scan


def _artifact(store, pending):
    art = store / "reflex" / "consolidation-proposal.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    data = {"proposals": [{"k": 1}], "applied": [], "rejected": []}
    if not pending:
        data["applied"] = [0]          # the lone proposal is handled
    art.write_text(json.dumps(data))


def _entry(staging: Path, name: str, title: str, body: str, ses: str) -> str:
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": ses, "observed_in_sessions": [ses],
          "tags": [], "tools": [], "confidence": 0.6}
    p = staging / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def test_auto_band_precleans_existing_queue_before_guard(tmp_path, monkeypatch):
    """With the auto flag ON, a relaunch clears the safe band from an EXISTING
    queue; the below-band remainder stays pending and regeneration stays frozen."""
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")
    monkeypatch.delenv("MAGNOLIA_CONSOLIDATION_AUTO_MAX", raising=False)
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    a0 = _entry(staging, "a0.md", "pair 0 variant A", "body alpha 0 xxx", "s0a")
    b0 = _entry(staging, "b0.md", "pair 0 variant B", "body beta 0", "s0b")
    a1 = _entry(staging, "a1.md", "pair 1 variant A", "body alpha 1 xxxx", "s1a")
    b1 = _entry(staging, "b1.md", "pair 1 variant B", "body beta 1", "s1b")
    art = store / "reflex" / "consolidation-proposal.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps({"proposals": [
        {"confidence": 0.6, "rationale": "same topic only",
         "sources": [a0, b0], "canonical": a0, "merged_preview": {}},
        {"confidence": 0.9, "rationale": "same claim",
         "sources": [a1, b1], "canonical": a1, "merged_preview": {}},
    ], "applied": [], "rejected": [], "rejected_keys": []}))
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    calls = {"n": 0}
    monkeypatch.setattr("compchem_memory.consolidation.consolidate_project_findings",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    startup_scan._maybe_consolidate(store)

    # Safe band cleared from the existing queue: 0.9 merged, receipt written.
    assert (staging / "a1.md").exists()
    assert (staging / "b1.md").exists() is False
    data = json.loads(art.read_text())
    assert data["applied"] == [1]
    rows = [json.loads(l) for l in
            (store / "reflex" / "consolidation-auto-log.jsonl").read_text().splitlines()]
    assert rows and rows[0]["requested"] == [1]
    # Below-band proposal untouched and still pending; no regeneration.
    assert (staging / "a0.md").exists() and (staging / "b0.md").exists()
    assert calls["n"] == 0


def test_preclean_is_noop_without_auto_flag(tmp_path, monkeypatch):
    """Without MAGNOLIA_CONSOLIDATION_AUTO the pre-clean changes nothing — the
    existing queue is left exactly as it was (soak-default behavior)."""
    monkeypatch.delenv("MAGNOLIA_CONSOLIDATION_AUTO", raising=False)
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    a0 = _entry(staging, "a0.md", "pair 0 variant A", "body alpha 0 xxx", "s0a")
    b0 = _entry(staging, "b0.md", "pair 0 variant B", "body beta 0", "s0b")
    art = store / "reflex" / "consolidation-proposal.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps({"proposals": [
        {"confidence": 0.9, "rationale": "same claim",
         "sources": [a0, b0], "canonical": a0, "merged_preview": {}}],
        "applied": [], "rejected": [], "rejected_keys": []}))
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    calls = {"n": 0}
    monkeypatch.setattr("compchem_memory.consolidation.consolidate_project_findings",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    startup_scan._maybe_consolidate(store)

    assert (staging / "a0.md").exists() and (staging / "b0.md").exists()
    assert calls["n"] == 0                       # guard still freezes regeneration
    assert not (store / "reflex" / "consolidation-auto-log.jsonl").exists()
    assert json.loads(art.read_text())["applied"] == []   # queue untouched


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
