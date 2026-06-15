# tests/test_startup_promotion.py
from pathlib import Path
import yaml
from compchem_memory import startup_scan


def _entry(entries, name, title, sessions):
    fm = {"title": title, "type": "success_pattern",
          "observed_in_sessions": sessions, "confidence": 0.9}
    (entries / name).write_text("---\n" + yaml.dump(fm) + "---\n\nbody\n")


def test_maybe_promote_runs_when_llm_available(tmp_path, monkeypatch):
    store = tmp_path / ".magnolia"; entries = store / "entries"; entries.mkdir(parents=True)
    _entry(entries, "a.md", "Alpha", ["s1", "s2", "s3"])
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    called = {}
    def fake_propose(store_dir, *, skills_dir):
        called["store"] = store_dir; return {"candidates": 0}
    monkeypatch.setattr("compchem_memory.promotion.propose_promotions", fake_propose)

    startup_scan._maybe_promote(store)
    assert called["store"] == str(store)


def test_maybe_promote_skips_without_llm(tmp_path, monkeypatch):
    store = tmp_path / ".magnolia"; (store / "entries").mkdir(parents=True)
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: False)
    startup_scan._maybe_promote(store)        # must not raise, must not call LLM
