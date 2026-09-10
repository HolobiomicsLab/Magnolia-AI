import time
from pathlib import Path
from compchem_tools.tools.jobs import _submit_local


def _wait_sentinel(path, timeout=8.0):
    p = Path(path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists() and p.read_text().strip():
            return p.read_text().strip()
        time.sleep(0.05)
    return None


def test_submit_local_writes_exit_sentinel_nonzero(tmp_path):
    res = _submit_local("sh -c 'exit 3'", tmp_path, "job", 1)
    assert res["success"] is True
    assert res["local_run_dir"] == str(tmp_path)
    code = _wait_sentinel(res["exit_sentinel"])
    assert code == "3"
    assert Path(res["exit_sentinel"]) == tmp_path / ".magnolia" / "local_exit_code"


def test_submit_local_writes_exit_sentinel_zero_and_logs(tmp_path):
    res = _submit_local("sh -c 'echo hello; exit 0'", tmp_path, "job", 2)
    code = _wait_sentinel(res["exit_sentinel"])
    assert code == "0"
    assert "hello" in (tmp_path / "job.out").read_text()


def test_submit_local_relative_wdir_still_writes_sentinel(tmp_path, monkeypatch):
    """Regression test: relative wdir must be resolved to absolute so sentinel is findable."""
    # Change to temp_path so we can use a relative subdir
    monkeypatch.chdir(tmp_path)

    # Create a relative subdir
    (tmp_path / "sub").mkdir()

    # Call _submit_local with a RELATIVE Path
    res = _submit_local("sh -c 'exit 4'", Path("sub"), "job", 1)
    assert res["success"] is True

    # Sentinel should be written with exit code "4"
    code = _wait_sentinel(res["exit_sentinel"])
    assert code == "4", f"Expected sentinel to contain '4', got '{code}'"

    # exit_sentinel path must be absolute (so poller can read it from any cwd)
    assert Path(res["exit_sentinel"]).is_absolute(), \
        f"exit_sentinel must be absolute, got: {res['exit_sentinel']}"


import yaml
from compchem_tools.tools.jobs import submit_job


def test_local_submit_records_remote_block_and_tags(tmp_path, monkeypatch):
    # isolate the run store to tmp
    import compchem_tools.tools.jobs as jobs_mod
    from compchem_memory.tiers.project import ProjectManager
    monkeypatch.setattr(jobs_mod, "_PROJECT_MANAGER", ProjectManager(tmp_path))
    proj = tmp_path / "proj"; proj.mkdir()

    out = submit_job("sh -c 'exit 0'", str(proj), scheduler="local",
                     tool="haddock3", project_dir=str(proj),
                     system_tags=["peptide", "6mer"], acknowledge=True)
    assert out["success"] is True
    runs = list((ProjectManager(tmp_path)._runs_dir(str(proj))).glob("*.yaml"))
    runs = [r for r in runs if r.name != "INDEX.yaml"]
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["remote"]["scheduler"] == "local"
    assert rec["remote"]["local_run_dir"] == str(proj)
    assert rec["remote"]["job_id"].startswith("local_")
    assert rec["remote"]["exit_sentinel"].endswith("local_exit_code")
    assert rec["system_tags"] == ["peptide", "6mer"]
    assert rec["lifecycle"] == "running"


def _alive_non_zombie(pid: int) -> bool:
    """Linux helper: True if pid exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(") ", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


def test_cancel_local_kills_children(tmp_path):
    """Regression (P0.2): cancel must kill the whole process group, not just
    the master. The job spawns a background child; both must die on cancel."""
    import time as _time
    from compchem_tools.tools.jobs import _cancel_local, _local_group_alive

    child_pidfile = tmp_path / "child.pid"
    res = _submit_local(
        f"sleep 300 & echo $! > {child_pidfile}; sleep 300",
        tmp_path, "job", 1,
    )
    assert res["success"] is True
    master_pid = res["pid"]

    # Wait for the child to spawn and record its pid.
    deadline = _time.time() + 5
    while _time.time() < deadline and not (
        child_pidfile.exists() and child_pidfile.read_text().strip()
    ):
        _time.sleep(0.05)
    assert child_pidfile.exists(), "child pidfile was never written"
    child_pid = int(child_pidfile.read_text().strip())

    assert _alive_non_zombie(master_pid), "master should be running before cancel"
    assert _alive_non_zombie(child_pid), "child should be running before cancel"

    cancel = _cancel_local(res["job_id"])
    assert cancel["success"] is True
    assert cancel["group_terminated"] is True, cancel

    deadline = _time.time() + 5
    while _time.time() < deadline and (
        _alive_non_zombie(master_pid) or _alive_non_zombie(child_pid)
    ):
        _time.sleep(0.05)
    assert not _alive_non_zombie(master_pid), "master survived cancel"
    assert not _alive_non_zombie(child_pid), "child survived cancel (original bug)"
    assert _local_group_alive(master_pid) is False


def test_cancel_local_already_terminated(tmp_path):
    """Cancel of a finished job succeeds without signalling errors."""
    import time as _time
    from compchem_tools.tools.jobs import _cancel_local

    res = _submit_local("sh -c 'exit 0'", tmp_path, "job", 1)
    assert res["success"] is True
    deadline = _time.time() + 5
    while _time.time() < deadline and _alive_non_zombie(res["pid"]):
        _time.sleep(0.05)

    cancel = _cancel_local(res["job_id"])
    assert cancel["success"] is True
    assert ("already terminated" in str(cancel.get("note", ""))
            or cancel.get("group_terminated") is True)
