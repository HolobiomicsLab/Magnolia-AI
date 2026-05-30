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


def test_poll_jobs_polls_each_active_run(tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r1", lifecycle="submitted", job_id="11")
    _write_run(runs, "r2", lifecycle="running",   job_id="22")
    _write_run(runs, "r_done", lifecycle="fetched", job_id="33")  # excluded

    seen_checks: list[str] = []
    def fake_check(*, job_id, cluster="azzurra", project_dir=None):
        seen_checks.append(job_id)
        return {"success": True, "state": "RUNNING", "lifecycle": "running",
                "terminal": False}
    monkeypatch.setattr(poller.ssh_slurm, "check", fake_check)
    monkeypatch.setattr(poller, "_PROJECT_MANAGER", _RecordingMgr())

    summary = poller.poll_jobs(str(pd))
    assert sorted(seen_checks) == ["11", "22"]
    assert summary["polled"] == 2
    assert summary["transitioned"] == 0


def test_poll_jobs_dispatches_terminal_runs(tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r1", lifecycle="running", job_id="11")
    monkeypatch.setattr(poller.ssh_slurm, "check",
        lambda *, job_id, cluster="azzurra", project_dir=None:
            {"success": True, "state": "COMPLETED", "lifecycle": "completed",
             "terminal": True, "exit_code": "0:0"})
    ssh = _StubSshSlurm()
    monkeypatch.setattr(poller.ssh_slurm, "fetch", ssh.fetch)
    monkeypatch.setattr(poller, "assess_and_record",
                        lambda **kw: {"overall": "pass"})
    monkeypatch.setattr(poller, "_PROJECT_MANAGER", _RecordingMgr())
    summary = poller.poll_jobs(str(pd))
    assert summary["polled"] == 1
    assert summary["transitioned"] == 1
    assert summary["assessed"] == 1
    assert ssh.fetch_calls == ["11"]


def test_poll_jobs_skips_when_lock_held(tmp_path, monkeypatch):
    """If a sweep is already running, return skipped without doing work."""
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "runs").mkdir(parents=True)
    monkeypatch.setattr(poller.ssh_slurm, "check",
                        lambda **kw: pytest.fail("should not be called"))
    with poller._SWEEP_LOCK:
        summary = poller.poll_jobs(str(pd))
    assert summary == {"skipped": "busy"}


def test_poll_jobs_one_bad_run_does_not_abort_sweep(tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "ok",  lifecycle="submitted", job_id="11")
    _write_run(runs, "bad", lifecycle="submitted", job_id="22")

    def fake_check(*, job_id, **kw):
        if job_id == "22":
            raise RuntimeError("simulated network")
        return {"success": True, "state": "RUNNING", "lifecycle": "running",
                "terminal": False}
    monkeypatch.setattr(poller.ssh_slurm, "check", fake_check)
    monkeypatch.setattr(poller, "_PROJECT_MANAGER", _RecordingMgr())
    summary = poller.poll_jobs(str(pd))
    # ok still polled; bad counted as an error but not raising
    assert summary["polled"] == 1
    assert summary["errors"] == 1


def test_resolve_poll_interval_default_when_unset(monkeypatch):
    monkeypatch.delenv("MAGNOLIA_POLL_INTERVAL_MIN", raising=False)
    assert poller._resolve_poll_interval_seconds() == 5 * 60


def test_resolve_poll_interval_uses_env(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_POLL_INTERVAL_MIN", "12")
    assert poller._resolve_poll_interval_seconds() == 12 * 60


def test_resolve_poll_interval_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_POLL_INTERVAL_MIN", "nonsense")
    assert poller._resolve_poll_interval_seconds() == 5 * 60
    monkeypatch.setenv("MAGNOLIA_POLL_INTERVAL_MIN", "0")
    assert poller._resolve_poll_interval_seconds() == 5 * 60
    monkeypatch.setenv("MAGNOLIA_POLL_INTERVAL_MIN", "-3")
    assert poller._resolve_poll_interval_seconds() == 5 * 60


def test_poll_tick_never_raises(monkeypatch):
    monkeypatch.setattr(poller, "poll_jobs",
                        lambda pd: (_ for _ in ()).throw(RuntimeError("kaboom")))
    poller._poll_tick("/no/where")  # must not raise


def test_run_poll_timer_does_startup_sweep_before_sleep(monkeypatch):
    """The worker must call _poll_tick BEFORE the first sleep, so a freshly
    re-opened opencode reconciles immediately instead of after one interval."""
    monkeypatch.setattr(poller, "_resolve_poll_interval_seconds", lambda: 0.01)
    monkeypatch.setattr(poller, "PROJECT_DIR_FOR_TIMER", "/p")
    calls: list[str] = []
    def fake_tick(pd):
        calls.append(pd)
        if len(calls) >= 2:
            raise SystemExit  # break the loop after two ticks
    monkeypatch.setattr(poller, "_poll_tick", fake_tick)
    try:
        poller._run_poll_timer_background_worker()
    except SystemExit:
        pass
    assert calls[0] == "/p"   # first tick is the startup sweep
    assert len(calls) >= 2


def test_dispatch_success_passes_run_id_explicitly(tmp_path, monkeypatch):
    """When the poller's success path assesses a finished run, the assessment
    must update the EXISTING run YAML (keyed on the magnolia run_id), not
    create an orphan keyed on basename(local_run_dir)."""
    mgr = _RecordingMgr()
    # _RecordingMgr doesn't implement record_run, so add a stub:
    mgr.records = []
    def record_run(project_dir, *, run_id, tool, status=None,
                    metrics=None, quality_flags=None, errors_solved=None,
                    lifecycle=None, remote=None):
        mgr.records.append({"run_id": run_id, "tool": tool, "status": status})
        return f"/fake/{run_id}.yaml"
    mgr.record_run = record_run

    ssh = _StubSshSlurm()
    monkeypatch.setattr(poller, "ssh_slurm", ssh)
    captured_run_ids: list[str] = []
    def fake_assess(*, run_dir, tool, exit_code, project_dir, project_mgr, run_id=None):
        captured_run_ids.append(run_id)
        return {"overall": "pass"}
    monkeypatch.setattr(poller, "assess_and_record", fake_assess)

    rec = _record_running()
    rec["run_id"] = "xtb_20260530_003349"  # magnolia run_id
    rec["remote"]["local_run_dir"] = str(tmp_path / "work_arbitrary")
    poller.dispatch_terminal(rec, {"state": "COMPLETED", "exit_code": "0:0",
                                    "terminal": True, "lifecycle": "completed"},
                              project_dir=str(tmp_path / "proj"), project_mgr=mgr)
    # The explicit run_id must be the magnolia-generated one, not basename(local_run_dir)
    assert captured_run_ids == ["xtb_20260530_003349"]
