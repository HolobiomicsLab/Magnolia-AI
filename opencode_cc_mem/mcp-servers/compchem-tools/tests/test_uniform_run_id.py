"""`submit_job` must return a run_id on EVERY backend (P1 uniform run_id).

The receipts extractor reads run_id from the recorded submit result — the
temporal join dissolves if a backend omits it. Before this, only ssh-slurm
minted one, and local/slurm/pbs attached it only when project_dir was given
AND recording succeeded. The same id must key the run record YAML, and the
run dir must resolve back to it (find_run_by_local_dir).
"""
import re
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from compchem_tools.tools import jobs

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9]+_\d{8}_\d{6}_[0-9a-f]{6}$")


@pytest.fixture
def project(tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    pd.mkdir()
    monkeypatch.setattr(
        jobs, "_PROJECT_MANAGER",
        jobs.ProjectManager(global_base=tmp_path / ".magnolia"),
    )
    return pd


def _records(pd):
    runs_dir = pd / ".magnolia" / "runs"
    if not runs_dir.is_dir():
        return []
    return [f for f in runs_dir.glob("*.yaml") if f.name != "INDEX.yaml"]


def _fake_scheduler(monkeypatch, binary, stdout):
    def fake_run(cmd, **kw):
        if cmd[0] == binary:
            return CompletedProcess(cmd, 0, stdout, "")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(jobs.subprocess, "run", fake_run)


def _assert_keyed_record(pd, result, work):
    assert result["success"] is True
    rid = result["run_id"]
    assert _RUN_ID_RE.fullmatch(rid), rid
    runs = _records(pd)
    assert len(runs) == 1, [f.name for f in runs]
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["run_id"] == rid
    assert runs[0].name.endswith(f"_{rid}.yaml")
    assert rec["remote"]["local_run_dir"] == str(work.resolve())
    resolved = jobs._PROJECT_MANAGER.find_run_by_local_dir(str(pd), str(work))
    assert resolved and resolved["run_id"] == rid
    return rec


def test_local_returns_run_id_and_keyed_record(project, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = jobs.submit_job(
        command="true", working_dir=str(work), scheduler="local",
        project_dir=str(project), tool="demo",
    )
    rec = _assert_keyed_record(project, result, work)
    assert rec["lifecycle"] == "running"
    assert rec["remote"]["scheduler"] == "local"


def test_slurm_returns_run_id_and_keyed_record(project, tmp_path, monkeypatch):
    _fake_scheduler(monkeypatch, "sbatch", "Submitted batch job 4242\n")
    work = tmp_path / "work"
    work.mkdir()
    result = jobs.submit_job(
        command="echo hi", working_dir=str(work), scheduler="slurm",
        project_dir=str(project), tool="demo",
    )
    rec = _assert_keyed_record(project, result, work)
    assert rec["lifecycle"] == "submitted"
    assert result["job_id"] == "4242"
    # Deliberately no scheduler key: status stays local-file based, and the
    # poller only tracks ssh-slurm/local records.
    assert "scheduler" not in rec["remote"]


def test_pbs_returns_run_id_and_keyed_record(project, tmp_path, monkeypatch):
    _fake_scheduler(monkeypatch, "qsub", "5150.server\n")
    work = tmp_path / "work"
    work.mkdir()
    result = jobs.submit_job(
        command="echo hi", working_dir=str(work), scheduler="pbs",
        project_dir=str(project), tool="demo",
    )
    rec = _assert_keyed_record(project, result, work)
    assert rec["lifecycle"] == "submitted"
    assert "scheduler" not in rec["remote"]


def test_no_project_dir_still_returns_run_id(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = jobs.submit_job(
        command="true", working_dir=str(work), scheduler="local", tool="demo",
    )
    assert result["success"] is True
    assert _RUN_ID_RE.fullmatch(result["run_id"])
    assert not (work / ".magnolia" / "runs").exists()


def test_record_failure_still_returns_run_id(project, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(jobs._PROJECT_MANAGER, "record_run", boom)
    result = jobs.submit_job(
        command="true", working_dir=str(work), scheduler="local",
        project_dir=str(project), tool="demo",
    )
    assert result["success"] is True
    assert _RUN_ID_RE.fullmatch(result["run_id"])
    assert _records(project) == []


def test_ssh_slurm_returns_run_id_and_keyed_record(project, tmp_path, monkeypatch):
    from compchem_tools.tools import ssh_slurm

    monkeypatch.setattr(
        ssh_slurm, "_PROJECT_MANAGER",
        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"),
    )

    def fake_run(cmd, *, capture_output=True, text=True, timeout=None, **kw):
        s = " ".join(cmd)
        if s.endswith("hpc_tunnel.sh"):
            return CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "rsync":
            return CompletedProcess(cmd, 0, "Number of regular files transferred: 1\n", "")
        if "sbatch job.slurm" in s:
            return CompletedProcess(cmd, 0, "Submitted batch job 777\n", "")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ssh_slurm.subprocess, "run", fake_run)
    work = tmp_path / "work"
    result = ssh_slurm.submit(
        command="xtb x.xyz", working_dir=str(work), project_dir=str(project),
        cluster="azzurra", tool="xtb",
    )
    assert result["success"] is True
    assert _RUN_ID_RE.fullmatch(result["run_id"])
    runs = _records(project)
    assert len(runs) == 1
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["run_id"] == result["run_id"]
    assert runs[0].name.endswith(f"_{result['run_id']}.yaml")


def test_one_generator_for_every_writer():
    from compchem_tools.tools import ssh_slurm

    assert jobs._generate_run_id is ssh_slurm._generate_run_id
