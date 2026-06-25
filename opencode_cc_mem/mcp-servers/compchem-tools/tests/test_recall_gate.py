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
