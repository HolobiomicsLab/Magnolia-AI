from compchem_tools.tools import jobs as jobs_mod
from compchem_tools.tools.jobs import submit_job


def _force_hold(monkeypatch):
    monkeypatch.setattr(
        jobs_mod, "recall_gate",
        lambda tool, command, project_dir, acknowledge:
            None if acknowledge else {"success": False, "held": True,
                                       "reason": "recall_gate", "tool": tool,
                                       "pitfalls": [{"title": "x"}],
                                       "instruction": "resubmit with acknowledge=True"},
    )


def test_unacknowledged_submit_is_held_and_does_not_launch(monkeypatch, tmp_path):
    _force_hold(monkeypatch)
    called = {"local": False}
    monkeypatch.setattr(jobs_mod, "_submit_local",
                        lambda *a, **k: called.__setitem__("local", True) or {"success": True})
    out = submit_job("python x.py", str(tmp_path), scheduler="local", tool="haddock3")
    assert out["held"] is True
    assert called["local"] is False  # launcher never invoked


def test_acknowledged_submit_launches(monkeypatch, tmp_path):
    _force_hold(monkeypatch)
    called = {"local": False}
    monkeypatch.setattr(jobs_mod, "_submit_local",
                        lambda *a, **k: called.__setitem__("local", True) or
                        {"success": True, "job_id": "local_1_abc", "scheduler": "local"})
    out = submit_job("python x.py", str(tmp_path), scheduler="local",
                     tool="haddock3", acknowledge=True)
    assert called["local"] is True
    assert out.get("success") is True


def test_gate_failure_does_not_block(monkeypatch, tmp_path):
    # recall_gate returning None (its fail-open contract) => normal launch
    monkeypatch.setattr(jobs_mod, "recall_gate", lambda *a, **k: None)
    called = {"local": False}
    monkeypatch.setattr(jobs_mod, "_submit_local",
                        lambda *a, **k: called.__setitem__("local", True) or
                        {"success": True, "job_id": "local_1_abc", "scheduler": "local"})
    submit_job("python x.py", str(tmp_path), scheduler="local", tool="haddock3")
    assert called["local"] is True
