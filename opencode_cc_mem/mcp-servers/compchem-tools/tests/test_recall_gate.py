from compchem_tools.tools import recall_gate as rg_mod
from compchem_tools.tools.recall_gate import recall_gate


def _hit():
    return {"title": "haddock3 OOM", "type": "failure_pattern",
            "provisional": False, "summary": "32 workers > 32GB", "source": "e1"}


def test_acknowledge_true_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [_hit()])
    assert recall_gate("haddock3", "cmd", "/proj", True) is None


def test_no_hits_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    assert recall_gate("haddock3", "cmd", "/proj", False) is None


def test_hits_return_hold_dict(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [_hit()])
    out = recall_gate("haddock3", "cmd", "/proj", False)
    assert out["success"] is False
    assert out["held"] is True
    assert out["reason"] == "recall_gate"
    assert out["tool"] == "haddock3"
    assert out["pitfalls"][0]["title"] == "haddock3 OOM"
    assert out["pitfalls"][0]["provisional"] is False
    assert "acknowledge=True" in out["instruction"]


def test_tool_none_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [_hit()])
    assert recall_gate(None, "cmd", "/proj", False) is None


def test_fail_open_on_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("memory down")
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", boom)
    assert recall_gate("haddock3", "cmd", "/proj", False) is None


def _sim():
    return {"run_id": "a", "run_dir": "/runs/a", "system_tags": ["peptide", "6mer"],
            "date": "2026-06-01", "status": "pass", "score": 2}


def test_similar_run_only_returns_hold(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    monkeypatch.setattr(rg_mod, "_similar_runs", lambda *a, **k: [_sim()])
    out = recall_gate("haddock3", "cmd", "/proj", False, system_tags=["peptide", "6mer"])
    assert out["held"] is True
    assert out["pitfalls"] == []
    assert out["similar_runs"][0]["run_dir"] == "/runs/a"
    assert "acknowledge=True" in out["instruction"]


def test_no_warnings_no_similar_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    monkeypatch.setattr(rg_mod, "_similar_runs", lambda *a, **k: [])
    assert recall_gate("haddock3", "cmd", "/proj", False, system_tags=["x"]) is None


def test_acknowledge_skips_similar(monkeypatch):
    monkeypatch.setattr(rg_mod, "_similar_runs", lambda *a, **k: [_sim()])
    assert recall_gate("haddock3", "cmd", "/proj", True, system_tags=["peptide"]) is None


def test_similar_runs_failure_is_fail_open(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    def boom(*a, **k):
        raise RuntimeError("history unreadable")
    monkeypatch.setattr(rg_mod, "_similar_runs", boom)
    assert recall_gate("haddock3", "cmd", "/proj", False, system_tags=["x"]) is None
