"""ssh_slurm against a site that is not Azzurra.

Every other test in this suite passes cluster="azzurra", so all of them would
still pass if the backend were hardcoded to it — which, until the registry
landed, it was. These tests exercise the paths a plain university cluster takes:
no VPN tunnel, no hand-opened ControlMaster, no group account, no modulefiles.
"""
from __future__ import annotations

from subprocess import CompletedProcess

import pytest
import yaml

from compchem_tools.tools import clusters, ssh_slurm

PLAIN = {"ssh_host": "login.example", "scratch_root": "/scratch/{user}/magnolia",
         "requires_control_master": False}


@pytest.fixture
def site(monkeypatch, tmp_path):
    """Install a one-cluster registry and hand back a way to amend it."""
    path = tmp_path / "clusters.yaml"
    monkeypatch.setattr(clusters, "PACKAGED_CLUSTERS", path)
    monkeypatch.setattr(clusters, "USER_CLUSTERS", tmp_path / "absent.yaml")
    monkeypatch.delenv(clusters.CLUSTERS_FILE_ENV, raising=False)
    monkeypatch.delenv(clusters.CLUSTER_ENV, raising=False)
    monkeypatch.setenv("USER", "someone")

    def install(**profiles):
        path.write_text(yaml.safe_dump({"clusters": profiles or {"plain": dict(PLAIN)}}))
        monkeypatch.setattr(ssh_slurm, "CLUSTER_CONFIG", clusters.load())

    install()
    return install


def _project(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / ".magnolia").mkdir(parents=True)
    return project_dir, tmp_path / "work"


def _submitting(fake_subprocess):
    fake_subprocess.canned["sbatch"] = CompletedProcess(
        args=[], returncode=0, stdout="Submitted batch job 99001\n", stderr="")
    return fake_subprocess


def _cmds(fake_subprocess):
    return [" ".join(c) for c in fake_subprocess.calls]


# ── steps a site does not have are skipped, not faked ───────────────────────

def test_no_tunnel_script_means_no_tunnel_step(site, fake_subprocess, tmp_path, monkeypatch):
    project_dir, work = _project(tmp_path)
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))

    _submitting(fake_subprocess)

    result = ssh_slurm.submit(command="echo hi", working_dir=str(work),
                              project_dir=str(project_dir), cluster="plain")

    assert result["success"] is True
    assert not any("hpc_tunnel" in c for c in _cmds(fake_subprocess))


def test_control_master_check_is_skipped_when_the_site_does_not_need_one(
        site, fake_subprocess, tmp_path, monkeypatch):
    """`ssh -O check` only makes sense where a human had to open a master for 2FA."""
    project_dir, work = _project(tmp_path)
    _submitting(fake_subprocess)
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))

    ssh_slurm.submit(command="echo hi", working_dir=str(work),
                     project_dir=str(project_dir), cluster="plain")

    assert not any("-O check" in c for c in _cmds(fake_subprocess))


def test_a_site_that_needs_a_master_still_gets_the_check(site, fake_subprocess, tmp_path):
    site(strict={**PLAIN, "requires_control_master": True})
    project_dir, work = _project(tmp_path)
    fake_subprocess.canned["-O check"] = CompletedProcess(args=[], returncode=255,
                                                          stdout="", stderr="")

    result = ssh_slurm.submit(command="echo hi", working_dir=str(work),
                              project_dir=str(project_dir), cluster="strict")

    assert result["error_kind"] == "master_down"
    assert "login.example" in result["error"]


# ── the generated script suits the site ─────────────────────────────────────

def test_a_bare_site_gets_a_script_slurm_will_accept(site, fake_subprocess, tmp_path,
                                                     monkeypatch):
    """An empty `#SBATCH --account=` is rejected; a bare `module use` fails set -e."""
    project_dir, work = _project(tmp_path)
    _submitting(fake_subprocess)
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))

    ssh_slurm.submit(command="echo hi", working_dir=str(work),
                     project_dir=str(project_dir), cluster="plain")

    script = (work / "job.slurm").read_text()
    assert "--account" not in script
    assert "--partition" not in script
    assert "module use" not in script
    assert "module purge" in script          # still a module-aware site


def test_the_scratch_path_uses_the_sites_layout_and_the_running_user(
        site, fake_subprocess, tmp_path, monkeypatch):
    project_dir, work = _project(tmp_path)
    _submitting(fake_subprocess)
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))

    result = ssh_slurm.submit(command="echo hi", working_dir=str(work),
                              project_dir=str(project_dir), cluster="plain")

    assert result["remote_run_dir"].startswith("/scratch/someone/magnolia/proj/runs/")


# ── resolution at the call site ─────────────────────────────────────────────

def test_cluster_is_optional_when_only_one_is_configured(site, fake_subprocess, tmp_path,
                                                         monkeypatch):
    project_dir, work = _project(tmp_path)
    _submitting(fake_subprocess)
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))

    result = ssh_slurm.submit(command="echo hi", working_dir=str(work),
                              project_dir=str(project_dir))

    assert result["success"] is True
    assert result["cluster"] == "plain"


def test_an_ambiguous_default_fails_before_touching_the_network(site, fake_subprocess,
                                                                tmp_path):
    site(a={**PLAIN, "ssh_host": "a"}, b={**PLAIN, "ssh_host": "b"})
    project_dir, work = _project(tmp_path)

    result = ssh_slurm.submit(command="echo hi", working_dir=str(work),
                              project_dir=str(project_dir))

    assert result["error_kind"] == "unknown_cluster"
    assert "a, b" in result["error"]
    assert fake_subprocess.calls == []


def test_check_resolves_the_cluster_too(site, fake_subprocess):
    fake_subprocess.canned["sacct"] = CompletedProcess(
        args=[], returncode=0,
        stdout="99001|COMPLETED|0:0|00:01:00|1024K|00:00:30|s|e|node01\n", stderr="")

    result = ssh_slurm.check(job_id="99001")

    assert result["success"] is True
    assert any("login.example" in c for c in _cmds(fake_subprocess))
