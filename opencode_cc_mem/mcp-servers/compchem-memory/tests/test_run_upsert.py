"""Run-record upsert: one file per run_id, assessment never erases remote/lifecycle.

Root cause of the run-log inconsistency: `assess_and_record` / `cmd_assess`
called `record_run` (CREATE, filename `{today}_{run_id}.yaml`). For a job that
completed the same day it was submitted, that overwrote the submit/poller record
in place — destroying the `remote` block (job_id, slurm state, dirs). Cross-day
it forked a twin. Fix: assessment upserts into the existing run_id record,
writing only its own non-empty fields.
"""

from pathlib import Path

import yaml
import pytest

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def store(tmp_path):
    ensure_project_store(str(tmp_path))
    return str(tmp_path)


def _pm():
    return ProjectManager(Path.home() / ".magnolia")


def test_upsert_merges_into_existing_and_preserves_remote(store):
    pm = _pm()
    # submit-style record: remote-tracked, no outcome yet
    pm.record_run(store, "haddock3_X", "haddock3", None, lifecycle="fetched",
                  remote={"cluster": "azzurra", "job_id": "123",
                          "slurm": {"state": "COMPLETED"}})
    # assessment upserts status + metrics
    pm.upsert_run(store, "haddock3_X", "haddock3", status="pass",
                  metrics={"cluster_count": 2})

    runs = list(pm._runs_dir(store).glob("*_haddock3_X.yaml"))
    assert len(runs) == 1, "must stay one file per run_id"
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["status"] == "pass"
    assert rec["remote"]["job_id"] == "123"            # not erased
    assert rec["remote"]["slurm"]["state"] == "COMPLETED"  # not erased
    assert rec["lifecycle"] == "fetched"               # not erased
    assert rec["metrics"]["cluster_count"] == 2        # merged in


def test_upsert_creates_when_absent(store):
    pm = _pm()
    pm.upsert_run(store, "local_Y", "haddock3", status="pass", metrics={"x": 1})
    runs = list(pm._runs_dir(store).glob("*_local_Y.yaml"))
    assert len(runs) == 1
    assert yaml.safe_load(runs[0].read_text())["status"] == "pass"


def test_upsert_does_not_fork_across_days(store):
    pm = _pm()
    runs_dir = pm._runs_dir(store)
    (runs_dir / "20260601_haddock3_Z.yaml").write_text(yaml.safe_dump(
        {"run_id": "haddock3_Z", "tool": "haddock3", "status": None,
         "date": "2026-06-01", "lifecycle": "fetched", "remote": {"job_id": "999"}}))
    pm.upsert_run(store, "haddock3_Z", "haddock3", status="pass", metrics={"a": 1})
    files = list(runs_dir.glob("*_haddock3_Z.yaml"))
    assert len(files) == 1, f"must not fork a second dated file; got {[f.name for f in files]}"
    rec = yaml.safe_load(files[0].read_text())
    assert rec["status"] == "pass" and rec["remote"]["job_id"] == "999"


def test_upsert_empty_values_do_not_erase(store):
    pm = _pm()
    pm.record_run(store, "haddock3_W", "haddock3", "pass", metrics={"keep": 1})
    # a no-op re-assess must not wipe existing status/metrics
    pm.upsert_run(store, "haddock3_W", "haddock3", status=None, metrics=None)
    rec = yaml.safe_load(list(pm._runs_dir(store).glob("*_haddock3_W.yaml"))[0].read_text())
    assert rec["status"] == "pass"          # not erased to None
    assert rec["metrics"]["keep"] == 1      # not erased


def test_assess_and_record_preserves_remote(store, monkeypatch):
    """The real bug path: poller assesses a same-day-completed job and must NOT
    destroy the remote block."""
    from compchem_memory.learning import orchestrator
    pm = _pm()
    pm.record_run(store, "haddock3_R", "haddock3", None, lifecycle="fetched",
                  remote={"job_id": "77", "slurm": {"state": "COMPLETED"}})
    monkeypatch.setattr(orchestrator, "assess_run",
                        lambda run_dir, tool, exit_code: {
                            "overall": "pass", "metrics": {"cluster_count": 3},
                            "quality_flags": []})
    orchestrator.assess_and_record(run_dir="/tmp/x", tool="haddock3", exit_code=0,
                                   project_dir=store, project_mgr=pm, run_id="haddock3_R")
    runs = list(pm._runs_dir(store).glob("*_haddock3_R.yaml"))
    assert len(runs) == 1
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["status"] == "pass"
    assert rec["remote"]["job_id"] == "77"   # the bug erased this; must survive
    assert rec["metrics"]["cluster_count"] == 3
