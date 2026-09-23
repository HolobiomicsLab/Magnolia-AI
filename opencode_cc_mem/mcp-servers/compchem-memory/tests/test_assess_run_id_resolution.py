"""Assessment must key on the run's canonical run_id, not fork a twin.

The join: submit paths pin ``remote.local_run_dir`` on the run record, so an
assessment given that run dir can reuse the record's run_id. Before this, the
MCP tool and CLI fell back to basename(run_dir) / a fresh mint, creating a
second YAML for the same run (P1 uniform run_id).
"""
from pathlib import Path

import yaml
import pytest

from compchem_memory.learning import orchestrator
from compchem_memory.storage import ensure_project_store
from compchem_memory.tiers.project import ProjectManager

_CANONICAL = "demo_20260923_120000_abc123"


@pytest.fixture
def store(tmp_path):
    ensure_project_store(str(tmp_path))
    return str(tmp_path)


def _pm():
    return ProjectManager(Path.home() / ".magnolia")


def _runs(store):
    return [
        f for f in _pm()._runs_dir(store).glob("*.yaml") if f.name != "INDEX.yaml"
    ]


def _patch_assess(monkeypatch, overall="pass"):
    monkeypatch.setattr(
        orchestrator,
        "assess_run",
        lambda run_dir, tool, exit_code: {
            "overall": overall,
            "metrics": {"x": 1},
            "quality_flags": [],
        },
    )


def _submit_record(store, run_dir):
    pm = _pm()
    pm.record_run(
        store,
        _CANONICAL,
        "demo",
        None,
        lifecycle="running",
        remote={"scheduler": "local", "local_run_dir": str(run_dir)},
    )


def test_assess_joins_submit_record_by_run_dir(store, monkeypatch):
    run_dir = Path(store) / "runs" / "2026-09-23_demo"
    run_dir.mkdir(parents=True)
    _submit_record(store, run_dir)
    _patch_assess(monkeypatch)

    orchestrator.assess_and_record(
        run_dir=str(run_dir), tool="demo", exit_code=0,
        project_dir=store, project_mgr=_pm(),
    )

    runs = _runs(store)
    assert len(runs) == 1, f"twin record forked: {[f.name for f in runs]}"
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["run_id"] == _CANONICAL
    assert rec["status"] == "pass"
    assert rec["remote"]["local_run_dir"] == str(run_dir)  # not erased


def test_assess_falls_back_to_basename_without_record(store, monkeypatch):
    run_dir = Path(store) / "runs" / "manual_run"
    run_dir.mkdir(parents=True)
    _patch_assess(monkeypatch)

    orchestrator.assess_and_record(
        run_dir=str(run_dir), tool="demo", exit_code=0,
        project_dir=store, project_mgr=_pm(),
    )

    recs = [yaml.safe_load(f.read_text()) for f in _runs(store)]
    assert [r["run_id"] for r in recs] == ["manual_run"]


def test_explicit_run_id_beats_resolution(store, monkeypatch):
    run_dir = Path(store) / "runs" / "2026-09-23_demo"
    run_dir.mkdir(parents=True)
    _submit_record(store, run_dir)
    _patch_assess(monkeypatch)

    orchestrator.assess_and_record(
        run_dir=str(run_dir), tool="demo", exit_code=0,
        project_dir=store, project_mgr=_pm(), run_id="explicit_id",
    )

    ids = {yaml.safe_load(f.read_text())["run_id"] for f in _runs(store)}
    assert ids == {_CANONICAL, "explicit_id"}


def test_resolve_run_id_is_the_public_join_helper(store):
    run_dir = Path(store) / "runs" / "2026-09-23_demo"
    run_dir.mkdir(parents=True)
    _submit_record(store, run_dir)
    assert orchestrator.resolve_run_id(_pm(), store, str(run_dir)) == _CANONICAL
    assert orchestrator.resolve_run_id(
        _pm(), store, str(Path(store) / "nope")
    ) == "nope"


def test_cli_assess_reuses_submit_record(store, monkeypatch):
    import argparse
    from compchem_memory import cli

    run_dir = Path(store) / "runs" / "2026-09-23_demo"
    run_dir.mkdir(parents=True)
    _submit_record(store, run_dir)
    monkeypatch.setattr(
        "compchem_memory.learning.assessor.assess_run",
        lambda run_dir, tool, exit_code: {
            "overall": "pass", "metrics": {}, "quality_flags": [],
        },
    )

    rc = cli.cmd_assess(argparse.Namespace(
        command=f"haddock3 {run_dir}", exit_code=0, project_dir=store,
    ))

    assert rc == 0
    runs = _runs(store)
    assert len(runs) == 1, f"CLI forked a twin: {[f.name for f in runs]}"
    assert yaml.safe_load(runs[0].read_text())["run_id"] == _CANONICAL


def test_cli_assess_mints_canonical_id_without_run_dir(store, monkeypatch):
    import argparse
    import re
    from compchem_memory import cli

    monkeypatch.setattr(cli, "_detect_run_dir", lambda command, tool: None)
    monkeypatch.setattr(
        "compchem_memory.learning.assessor.assess_run",
        lambda run_dir, tool, exit_code: {
            "overall": "pass", "metrics": {}, "quality_flags": [],
        },
    )

    rc = cli.cmd_assess(argparse.Namespace(
        command="haddock3 config.cfg", exit_code=0, project_dir=store,
    ))

    assert rc == 0
    rid = yaml.safe_load(_runs(store)[0].read_text())["run_id"]
    assert re.fullmatch(r"haddock3_\d{8}_\d{6}_[0-9a-f]{6}", rid), rid
