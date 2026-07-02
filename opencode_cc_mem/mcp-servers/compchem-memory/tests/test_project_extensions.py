"""Tests for Sub-project B's ProjectManager extensions.

Covers:
  - record_run(lifecycle=..., remote=...) accepts and persists the new fields
  - record_run without new kwargs still produces the legacy record (backward-compat)
  - update_run(patch=...) deep-merges into an existing record
  - update_run raises FileNotFoundError when the record is missing
  - _update_runs_index emits flow-style YAML with the new columns
  - The INDEX is greppable: searching for `cluster: azzurra` returns
    a self-contained one-line record
"""
from __future__ import annotations
from pathlib import Path
import yaml
import pytest
from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def project(tmp_path: Path) -> tuple[ProjectManager, str]:
    """A fresh ProjectManager pointed at a tmp project dir, with .magnolia/ scaffolded."""
    project_dir = tmp_path / "myproject"
    (project_dir / ".magnolia" / "runs").mkdir(parents=True)
    pm = ProjectManager(global_base=tmp_path / "global")
    return pm, str(project_dir)


def test_record_run_with_lifecycle_and_remote_persists_fields(project):
    pm, project_dir = project
    pm.record_run(
        project_dir=project_dir,
        run_id="haddock3_20260529_140000",
        tool="haddock3",
        status=None,
        lifecycle="submitted",
        remote={
            "scheduler": "ssh-slurm",
            "cluster": "azzurra",
            "job_id": "11331448",
            "account": "spectrometry",
            "qos": "qos_spectrometry",
            "partition": "cpucourt",
            "local_run_dir": "runs/haddock3_20260529_140000",
            "remote_run_dir": "/workspace/tjiang/magnolia/myproject/runs/haddock3_20260529_140000",
            "submitted_at": "2026-05-29T14:00:00+00:00",
        },
    )
    yaml_files = list((Path(project_dir) / ".magnolia" / "runs").glob("*_haddock3_20260529_140000.yaml"))
    assert len(yaml_files) == 1
    record = yaml.safe_load(yaml_files[0].read_text())
    assert record["lifecycle"] == "submitted"
    assert record["remote"]["cluster"] == "azzurra"
    assert record["remote"]["job_id"] == "11331448"
    assert record["status"] is None


def test_record_run_without_new_kwargs_writes_legacy_record(project):
    pm, project_dir = project
    pm.record_run(
        project_dir=project_dir,
        run_id="xtb_20260420_103000",
        tool="xtb",
        status="pass",
        metrics={"energy": -76.4},
        quality_flags=[],
    )
    yaml_files = list((Path(project_dir) / ".magnolia" / "runs").glob("*_xtb_20260420_103000.yaml"))
    assert len(yaml_files) == 1
    record = yaml.safe_load(yaml_files[0].read_text())
    assert "lifecycle" not in record
    assert "remote" not in record
    assert record["status"] == "pass"
    assert record["metrics"]["energy"] == -76.4


def test_update_run_deep_merges_patch(project):
    pm, project_dir = project
    pm.record_run(
        project_dir=project_dir,
        run_id="haddock3_20260529_140000",
        tool="haddock3",
        status=None,
        lifecycle="submitted",
        remote={
            "cluster": "azzurra",
            "job_id": "11331448",
            "account": "spectrometry",
        },
    )
    pm.update_run(
        project_dir=project_dir,
        run_id="haddock3_20260529_140000",
        patch={
            "lifecycle": "running",
            "remote": {
                "slurm": {"state": "RUNNING", "elapsed": "00:00:10"},
                "last_polled_at": "2026-05-29T14:01:00+00:00",
            },
        },
    )
    yaml_files = list((Path(project_dir) / ".magnolia" / "runs").glob("*_haddock3_20260529_140000.yaml"))
    record = yaml.safe_load(yaml_files[0].read_text())
    assert record["lifecycle"] == "running"
    assert record["remote"]["account"] == "spectrometry"
    assert record["remote"]["cluster"] == "azzurra"
    assert record["remote"]["job_id"] == "11331448"
    assert record["remote"]["last_polled_at"] == "2026-05-29T14:01:00+00:00"
    assert record["remote"]["slurm"]["state"] == "RUNNING"
    assert record["remote"]["slurm"]["elapsed"] == "00:00:10"


def test_update_run_raises_when_record_missing(project):
    pm, project_dir = project
    with pytest.raises(FileNotFoundError, match="No run record for run_id='ghost_999'"):
        pm.update_run(project_dir=project_dir, run_id="ghost_999", patch={"lifecycle": "running"})


def test_index_emits_flow_style_one_record_per_line(project):
    pm, project_dir = project
    pm.record_run(
        project_dir=project_dir,
        run_id="haddock3_20260529_140000",
        tool="haddock3",
        status=None,
        lifecycle="completed",
        remote={
            "cluster": "azzurra",
            "job_id": "11331448",
            "slurm": {"state": "COMPLETED", "elapsed": "00:00:42", "node_list": "gpu06"},
        },
    )
    pm.record_run(
        project_dir=project_dir,
        run_id="xtb_20260420_103000",
        tool="xtb",
        status="pass",
    )
    index_path = Path(project_dir) / ".magnolia" / "runs" / "INDEX.yaml"
    assert index_path.exists()
    text = index_path.read_text()
    record_lines = [line for line in text.splitlines() if line.startswith("- {")]
    assert len(record_lines) == 2, f"expected 2 single-line records, got: {text!r}"
    azzurra_lines = [line for line in record_lines if "azzurra" in line]
    assert len(azzurra_lines) == 1
    line = azzurra_lines[0]
    assert "haddock3_20260529_140000" in line
    assert "tool: haddock3" in line
    assert "cluster: azzurra" in line
    assert "job_id: '11331448'" in line or "job_id: 11331448" in line
    assert "slurm_state: COMPLETED" in line
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, list)
    assert len(parsed) == 2


def test_index_grep_for_state_returns_complete_record(project):
    pm, project_dir = project
    pm.record_run(
        project_dir=project_dir,
        run_id="haddock3_20260530_090000",
        tool="haddock3",
        status=None,
        lifecycle="failed",
        remote={
            "cluster": "azzurra",
            "job_id": "11331500",
            "slurm": {"state": "TIMEOUT", "elapsed": "01:30:00", "node_list": "compute41"},
        },
    )
    index_path = Path(project_dir) / ".magnolia" / "runs" / "INDEX.yaml"
    text = index_path.read_text()
    matching = [line for line in text.splitlines() if "TIMEOUT" in line]
    assert len(matching) == 1
    line = matching[0]
    for token in ["haddock3_20260530_090000", "cluster: azzurra", "job_id: '11331500'", "elapsed: 01:30:00", "node: compute41"]:
        assert token in line, f"missing {token!r} in {line!r}"
