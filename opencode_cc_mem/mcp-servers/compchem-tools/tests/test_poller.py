"""Poller policy: scan, dispatch, capture. Pure orchestration on top of
ssh_slurm primitives and ProjectManager. All network I/O is stubbed."""
from pathlib import Path
import yaml
import pytest

from compchem_tools.tools import poller


def _write_run(runs_dir: Path, run_id: str, *, lifecycle: str | None,
                scheduler: str | None = "ssh-slurm", job_id: str | None = "100"):
    runs_dir.mkdir(parents=True, exist_ok=True)
    rec = {"run_id": run_id, "tool": "xtb", "status": None,
            "date": "2026-05-29", "metrics": {}, "quality_flags": [], "errors_solved": []}
    if lifecycle is not None:
        rec["lifecycle"] = lifecycle
    if scheduler is not None:
        rec["remote"] = {"scheduler": scheduler, "cluster": "azzurra",
                          "job_id": job_id, "local_run_dir": str(runs_dir / run_id),
                          "remote_run_dir": f"/r/{run_id}"}
    (runs_dir / f"20260529_{run_id}.yaml").write_text(yaml.dump(rec))


def test_scan_active_returns_only_ssh_slurm_submitted_or_running(tmp_path):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r_submitted", lifecycle="submitted")
    _write_run(runs, "r_running",   lifecycle="running")
    _write_run(runs, "r_completed", lifecycle="completed")
    _write_run(runs, "r_fetched",   lifecycle="fetched")
    _write_run(runs, "r_cancelled", lifecycle="cancelled")
    _write_run(runs, "r_submitting", lifecycle="submitting")  # no job_id yet → skip
    _write_run(runs, "r_local",     lifecycle="submitted", scheduler="slurm")  # not ssh-slurm
    _write_run(runs, "r_nolife",    lifecycle=None)  # absent lifecycle (legacy local-only)

    active = poller._scan_active_runs(str(pd))
    ids = {r["run_id"] for r in active}
    assert ids == {"r_submitted", "r_running"}


def test_scan_active_skips_corrupt_yamls(tmp_path):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r_ok", lifecycle="submitted")
    (runs / "20260529_broken.yaml").write_text("a: [unterminated\n")
    (runs / "20260529_scalar.yaml").write_text("just-a-string\n")
    active = poller._scan_active_runs(str(pd))  # no crash
    assert {r["run_id"] for r in active} == {"r_ok"}


def test_scan_active_skips_runs_missing_job_id(tmp_path):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r_ok", lifecycle="submitted")
    _write_run(runs, "r_nojob", lifecycle="submitted", job_id=None)
    active = poller._scan_active_runs(str(pd))
    assert {r["run_id"] for r in active} == {"r_ok"}


def test_scan_active_returns_empty_when_runs_dir_absent(tmp_path):
    pd = tmp_path / "proj"
    pd.mkdir()
    assert poller._scan_active_runs(str(pd)) == []


class _RecordingMgr:
    """Stub ProjectManager that records calls."""
    def __init__(self):
        self.updates: list[dict] = []
        self.entries: list[dict] = []
    def update_run(self, project_dir, run_id, patch):
        self.updates.append({"run_id": run_id, "patch": patch})
        return f"/fake/{run_id}.yaml"
    def create_entry(self, project_dir, title, content, *, tags=None,
                     source="auto", staging=False, entry_type="note", **_kw):
        self.entries.append({"title": title, "content": content,
                              "tags": tags or [], "staging": staging,
                              "entry_type": entry_type, "source": source})
        return f"/fake/staging/{title}.md"


def test_capture_failure_writes_yaml_patch_and_staging_entry(tmp_path):
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    (run_dir / "job.err").write_text("\n".join(f"err line {i}" for i in range(80)) + "\n")
    (run_dir / "job.out").write_text("\n".join(f"out line {i}" for i in range(80)) + "\n")
    mgr = _RecordingMgr()
    poller.capture_failure(
        project_dir=str(tmp_path / "proj"),
        run_id="r1",
        tool="xtb",
        local_run_dir=run_dir,
        state="FAILED",
        exit_code="1:0",
        project_mgr=mgr,
    )
    # YAML patch
    assert len(mgr.updates) == 1
    patch = mgr.updates[0]["patch"]
    assert patch["lifecycle"] == "fetched"
    fail = patch["remote"]["failure"]
    assert fail["state"] == "FAILED"
    assert fail["exit_code"] == "1:0"
    assert "err line 79" in fail["err_tail"]  # last lines kept
    assert "out line 79" in fail["out_tail"]
    assert fail["captured_at"]
    # Staging entry
    assert len(mgr.entries) == 1
    entry = mgr.entries[0]
    assert entry["staging"] is True
    assert "r1" in entry["title"]
    assert "FAILED" in entry["content"]
    assert "xtb" in entry["tags"]


def test_capture_failure_handles_missing_log_files(tmp_path):
    run_dir = tmp_path / "rd"
    run_dir.mkdir()  # no .err / .out
    mgr = _RecordingMgr()
    poller.capture_failure(
        project_dir=str(tmp_path / "proj"),
        run_id="r2",
        tool="xtb",
        local_run_dir=run_dir,
        state="TIMEOUT",
        exit_code="0:0",
        project_mgr=mgr,
    )
    fail = mgr.updates[0]["patch"]["remote"]["failure"]
    assert fail["err_tail"] == ""
    assert fail["out_tail"] == ""
    assert fail["state"] == "TIMEOUT"


class _StubSshSlurm:
    def __init__(self):
        self.fetch_calls = []
    def fetch(self, *, job_id, project_dir):
        self.fetch_calls.append(job_id)
        return {"success": True, "files_fetched": 3}


def _record_running(scheduler="ssh-slurm", **extra):
    rec = {
        "run_id": "r1", "tool": "xtb", "lifecycle": "running",
        "remote": {
            "scheduler": scheduler, "cluster": "azzurra",
            "job_id": "777", "local_run_dir": "/some/local", "remote_run_dir": "/r/777",
        },
    }
    rec.update(extra)
    return rec


def test_dispatch_completed_calls_fetch_then_assess(tmp_path, monkeypatch):
    mgr = _RecordingMgr()
    ssh = _StubSshSlurm()
    monkeypatch.setattr(poller, "ssh_slurm", ssh)
    assess_called = []
    def fake_assess(**kw):
        assess_called.append(kw)
        return {"overall": "pass", "metrics": {}, "quality_flags": []}
    monkeypatch.setattr(poller, "assess_and_record", fake_assess)

    rec = _record_running()
    rec["remote"]["local_run_dir"] = str(tmp_path)
    poller.dispatch_terminal(rec, {"state": "COMPLETED", "exit_code": "0:0",
                                    "terminal": True, "lifecycle": "completed"},
                              project_dir=str(tmp_path / "proj"), project_mgr=mgr)
    assert ssh.fetch_calls == ["777"]
    assert len(assess_called) == 1
    assert assess_called[0]["tool"] == "xtb"
    assert assess_called[0]["exit_code"] == 0


def test_dispatch_failed_calls_fetch_then_capture(tmp_path, monkeypatch):
    mgr = _RecordingMgr()
    ssh = _StubSshSlurm()
    monkeypatch.setattr(poller, "ssh_slurm", ssh)
    monkeypatch.setattr(poller, "assess_and_record",
                        lambda **kw: pytest.fail("must not assess on failure"))
    rec = _record_running()
    rec["remote"]["local_run_dir"] = str(tmp_path)
    poller.dispatch_terminal(rec, {"state": "FAILED", "exit_code": "1:0",
                                    "terminal": True, "lifecycle": "failed"},
                              project_dir=str(tmp_path / "proj"), project_mgr=mgr)
    assert ssh.fetch_calls == ["777"]
    # capture_failure → one update + one staging entry
    assert any(u["patch"].get("remote", {}).get("failure") for u in mgr.updates)
    assert len(mgr.entries) == 1


def test_dispatch_node_fail_no_fetch_flags_retry(tmp_path, monkeypatch):
    mgr = _RecordingMgr()
    ssh = _StubSshSlurm()
    monkeypatch.setattr(poller, "ssh_slurm", ssh)
    rec = _record_running()
    poller.dispatch_terminal(rec, {"state": "NODE_FAIL", "exit_code": "0:0",
                                    "terminal": True, "lifecycle": "failed"},
                              project_dir=str(tmp_path / "proj"), project_mgr=mgr)
    assert ssh.fetch_calls == []
    assert any(u["patch"].get("remote", {}).get("retry_recommended") is True
                for u in mgr.updates)


def test_dispatch_cancelled_no_fetch(tmp_path, monkeypatch):
    mgr = _RecordingMgr()
    ssh = _StubSshSlurm()
    monkeypatch.setattr(poller, "ssh_slurm", ssh)
    rec = _record_running()
    poller.dispatch_terminal(rec, {"state": "CANCELLED", "exit_code": "0:0",
                                    "terminal": True, "lifecycle": "cancelled"},
                              project_dir=str(tmp_path / "proj"), project_mgr=mgr)
    assert ssh.fetch_calls == []
    # Just a state-record update (lifecycle update was already made by check())
    # — no failure, no retry_recommended.
    assert not any(u["patch"].get("remote", {}).get("failure") for u in mgr.updates)


def test_dispatch_unknown_state_treated_as_science_failure(tmp_path, monkeypatch):
    mgr = _RecordingMgr()
    ssh = _StubSshSlurm()
    monkeypatch.setattr(poller, "ssh_slurm", ssh)
    rec = _record_running()
    rec["remote"]["local_run_dir"] = str(tmp_path)
    poller.dispatch_terminal(rec, {"state": "WEIRDNESS", "exit_code": "?",
                                    "terminal": True, "lifecycle": "failed"},
                              project_dir=str(tmp_path / "proj"), project_mgr=mgr)
    assert ssh.fetch_calls == ["777"]  # conservative: fetch + capture
    assert len(mgr.entries) == 1
