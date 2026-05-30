"""End-to-end integration test against the live Azzurra cluster.

Gated by MAGNOLIA_INTEGRATION_AZZURRA=1 because it requires:
  - The hpc_tunnel.sh-based tunnel pattern from Sub-project A
  - SSH access to Azzurra
  - The pass entry, sudoers, etc. set up

Run with:
    MAGNOLIA_INTEGRATION_AZZURRA=1 pytest tests/test_ssh_slurm_integration.py -v -s
"""
from __future__ import annotations
import os
import time
import json
from pathlib import Path
import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("MAGNOLIA_INTEGRATION_AZZURRA"),
    reason="set MAGNOLIA_INTEGRATION_AZZURRA=1 to run against live cluster",
)


def test_ssh_slurm_end_to_end_against_real_azzurra(tmp_path):
    from compchem_tools.tools import ssh_slurm

    project_dir = tmp_path / "magnolia-int-test"
    (project_dir / ".magnolia" / "runs").mkdir(parents=True)
    local_run_dir = project_dir / "runs" / "int_test"
    local_run_dir.mkdir(parents=True)
    (local_run_dir / "marker.txt").write_text("hello from integration test\n")

    # Submit
    submitted = ssh_slurm.submit(
        command='echo "hello from azzurra" && date && cat marker.txt',
        working_dir=str(local_run_dir),
        project_dir=str(project_dir),
        cluster="azzurra",
        partition="gpu",
        ncores=1,
        memory="1GB",
        time_limit="00:05:00",
        job_name="magnolia_int_test",
        tool=None,
    )
    print(f"submit -> {submitted}")
    assert submitted["success"] is True, f"submit failed: {submitted}"
    job_id = submitted["job_id"]

    # Poll to terminal — up to 5 minutes
    deadline = time.time() + 300
    while time.time() < deadline:
        checked = ssh_slurm.check(
            job_id=job_id,
            cluster="azzurra",
            project_dir=str(project_dir),
        )
        print(f"check -> {checked.get('state')} / {checked.get('lifecycle')}")
        if checked.get("terminal"):
            break
        time.sleep(15)
    else:
        pytest.fail(f"job {job_id} did not reach terminal state within 5 minutes")

    assert checked["state"] in ("COMPLETED", "CD"), f"expected COMPLETED, got {checked}"

    # Fetch results
    fetched = ssh_slurm.fetch(
        job_id=job_id,
        project_dir=str(project_dir),
    )
    print(f"fetch -> {fetched}")
    assert fetched["success"] is True
    out_files = list(local_run_dir.glob("magnolia_int_test_*.out"))
    assert len(out_files) == 1
    out = out_files[0].read_text()
    assert "hello from azzurra" in out
    assert "hello from integration test" in out

    import yaml as yamlpkg
    yaml_files = list((project_dir / ".magnolia" / "runs").glob("*.yaml"))
    yaml_files = [f for f in yaml_files if f.name != "INDEX.yaml"]
    assert len(yaml_files) == 1
    rec = yamlpkg.safe_load(yaml_files[0].read_text())
    assert rec["lifecycle"] == "fetched"
    assert rec["remote"]["slurm"]["state"] in ("COMPLETED", "CD")
    assert "fetched_at" in rec["remote"]


def test_poll_drives_short_job_to_fetched(tmp_path):
    """Submit a 5s `true` job; loop poll_jobs() until terminal; assert
    lifecycle=fetched + assessment recorded.

    Gated by MAGNOLIA_INTEGRATION_AZZURRA=1; takes 1-3 minutes."""
    import os
    if os.environ.get("MAGNOLIA_INTEGRATION_AZZURRA") != "1":
        import pytest; pytest.skip("set MAGNOLIA_INTEGRATION_AZZURRA=1 to run")
    import time, yaml
    from pathlib import Path
    from compchem_tools.tools import ssh_slurm, poller
    pd = tmp_path / "proj"
    pd.mkdir()
    work = tmp_path / "work"
    submit = ssh_slurm.submit(
        command="sleep 5 && echo ok",
        working_dir=str(work),
        project_dir=str(pd),
        tool=None,
        job_name="csweep_ok",
        ncores=1, memory="1GB", time_limit="00:05:00",
    )
    assert submit["success"], submit
    job_id = submit["job_id"]
    deadline = time.time() + 180  # 3 min cap
    last_summary = None
    rec = None
    while time.time() < deadline:
        last_summary = poller.poll_jobs(str(pd))
        # Inspect the run YAML
        runs = [p for p in (pd / ".magnolia" / "runs").glob("*.yaml")
                if p.name != "INDEX.yaml"]
        rec = yaml.safe_load(runs[0].read_text())
        if rec.get("lifecycle") == "fetched":
            break
        time.sleep(10)
    assert rec is not None
    assert rec["lifecycle"] == "fetched", f"final: {rec}"
    # Assessment ran → status set
    assert rec.get("status") in ("pass", "warning", "fail")


def test_poll_captures_failed_job(tmp_path):
    """A job that exits non-zero must end with lifecycle=fetched, a
    structured failure record, and a staging memory entry."""
    import os
    if os.environ.get("MAGNOLIA_INTEGRATION_AZZURRA") != "1":
        import pytest; pytest.skip("set MAGNOLIA_INTEGRATION_AZZURRA=1 to run")
    import time, yaml
    from compchem_tools.tools import ssh_slurm, poller
    pd = tmp_path / "proj"; pd.mkdir()
    work = tmp_path / "work"
    submit = ssh_slurm.submit(
        command="echo failing-on-purpose >&2; exit 7",
        working_dir=str(work),
        project_dir=str(pd),
        tool=None,
        job_name="csweep_fail",
        ncores=1, memory="1GB", time_limit="00:05:00",
    )
    assert submit["success"], submit
    deadline = time.time() + 180
    rec = None
    while time.time() < deadline:
        poller.poll_jobs(str(pd))
        runs = [p for p in (pd / ".magnolia" / "runs").glob("*.yaml")
                if p.name != "INDEX.yaml"]
        rec = yaml.safe_load(runs[0].read_text())
        if rec.get("lifecycle") == "fetched":
            break
        time.sleep(10)
    assert rec is not None
    assert rec["lifecycle"] == "fetched"
    fail = rec.get("remote", {}).get("failure")
    assert fail, f"expected remote.failure, got {rec}"
    assert fail["state"] == "FAILED"
    assert "failing-on-purpose" in fail.get("err_tail", "") + fail.get("out_tail", "")
    # Staging entry present
    staging_dir = pd / ".magnolia" / "staging"
    assert any(p.name.endswith(".md") for p in staging_dir.glob("*.md"))
