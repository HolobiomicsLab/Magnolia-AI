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
