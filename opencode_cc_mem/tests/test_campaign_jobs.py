import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.jobs import (  # noqa: E402
    CompchemJobBackend,
    FakeJobBackend,
    JobController,
    JobError,
    JobIntegrityError,
    JobStatus,
    load_job_plan,
)
from campaign_runner.state import CampaignStore  # noqa: E402


def protocol_payload():
    return {
        "schema": 1,
        "id": "job-pilot",
        "project": "hsc70_new",
        "worker": {
            "model": "opencode-go/deepseek-v4.1-flash",
            "agent": "campaign-worker",
        },
        "reviewer": {"enabled": False},
        "budget": {
            "max_tokens": 1000,
            "max_wall_hours": 1,
            "max_core_hours": 10,
            "max_attempts_per_stage": 2,
            "max_parallel_jobs": 2,
            "max_followups": 0,
        },
        "stages": [
            {
                "id": "dock",
                "type": "job",
                "command": "two-jobs",
                "gate": ["/bin/true"],
            }
        ],
    }


def make_store(tmp_path):
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps(protocol_payload()), encoding="utf-8")
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    store = CampaignStore(tmp_path / "project" / "runs" / "job-run")
    store.initialize(protocol, brief)
    jobs_dir = store.control_dir / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "two-jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "key": "parent",
                        "command": [sys.executable, "-c", "print('parent')"],
                        "working_dir": "work/parent",
                        "submit": {"scheduler": "local", "tool": "haddock3"},
                    },
                    {
                        "key": "nle",
                        "command": [sys.executable, "-c", "print('nle')"],
                        "working_dir": "work/nle",
                        "submit": {"scheduler": "local", "tool": "haddock3"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return store, protocol, brief


def test_submit_poll_and_no_duplicate(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend()
    project = tmp_path / "project"
    controller = JobController(store, project, backend)
    stage = store.verify_protocol().stage("dock")
    success, detail = controller.submit(stage, 1)
    assert success is True
    assert "2 job" in detail
    assert len(backend.submitted) == 2
    assert all(request.working_dir.is_dir() for request in backend.submitted)
    success, _ = controller.submit(stage, 1)
    assert success is True
    assert len(backend.submitted) == 2
    outcome = controller.poll(stage, 1)
    assert outcome.terminal is True
    assert outcome.success is True


def test_poll_pending_then_success(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend(
        statuses={
            "fake-1": [
                JobStatus(terminal=False, state="RUNNING"),
                JobStatus(terminal=True, success=True, state="COMPLETED"),
            ],
            "fake-2": [
                JobStatus(terminal=False, state="RUNNING"),
                JobStatus(terminal=True, success=True, state="COMPLETED"),
            ],
        }
    )
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    first = controller.poll(stage, 1)
    assert first.terminal is False
    second = controller.poll(stage, 1)
    assert second.terminal is True
    assert second.success is True


def test_infrastructure_failure_is_reported(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend(
        statuses={
            "fake-1": [JobStatus(terminal=True, success=False, category="infra", state="FAILED")],
            "fake-2": [JobStatus(terminal=True, success=True, category="success", state="COMPLETED")],
        }
    )
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    outcome = controller.poll(stage, 1)
    assert outcome.terminal is True
    assert outcome.success is False
    assert outcome.category == "infra"


def test_parallel_job_limit_is_enforced(tmp_path):
    store, _, _ = make_store(tmp_path)
    controller = JobController(
        store,
        tmp_path / "project",
        FakeJobBackend(),
        max_parallel_jobs=1,
    )
    stage = store.verify_protocol().stage("dock")
    with pytest.raises(JobError, match="limit is 1"):
        controller.submit(stage, 1)


def test_cancel_all_changes_terminal_jobs(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend()
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    cancelled = controller.cancel_all("test")
    assert len(cancelled) == 2
    assert len(backend.cancelled) == 2


def test_failed_job_cancels_running_siblings(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend(
        statuses={
            "fake-1": [
                JobStatus(
                    terminal=True,
                    success=False,
                    category="infra",
                    state="FAILED",
                )
            ],
            "fake-2": [JobStatus(terminal=False, state="RUNNING")],
        }
    )
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    outcome = controller.poll(stage, 1)
    assert outcome.terminal is True
    assert outcome.category == "infra"
    assert backend.cancelled == ["fake-2"]
    jobs = json.loads(controller.path.read_text())
    assert jobs["dock/1/parent"]["status"] == "failed"
    assert jobs["dock/1/nle"]["status"] == "cancelled"


def test_partial_submission_failure_cancels_submitted_jobs(tmp_path):
    class PartialBackend(FakeJobBackend):
        def submit(self, request, project_dir):
            if self.submitted:
                return {"success": False, "error": "second submission failed"}
            return super().submit(request, project_dir)

    store, _, _ = make_store(tmp_path)
    backend = PartialBackend()
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    success, detail = controller.submit(stage, 1)
    assert success is False
    assert "second submission failed" in detail
    assert backend.cancelled == ["fake-1"]
    jobs = json.loads(controller.path.read_text())
    assert jobs["dock/1/parent"]["status"] == "cancelled"
    assert jobs["dock/1/nle"]["status"] == "submit_failed"


def test_hash_mismatch_causes_zero_submissions(tmp_path):
    store, _, _ = make_store(tmp_path)
    controller = JobController(store, tmp_path / "project", FakeJobBackend())
    controller.path.write_text(
        json.dumps(
            {
                "dock/1/nle": {
                    "key": "dock/1/nle",
                    "stage": "dock",
                    "attempt": 1,
                    "run_id": "old-run",
                    "status": "submitted",
                    "command_sha256": "wrong",
                }
            }
        )
    )
    stage = store.verify_protocol().stage("dock")
    with pytest.raises(JobIntegrityError, match="command hash changed"):
        controller.submit(stage, 1)
    assert controller.backend.submitted == []


def test_active_jobs_across_attempts_count_toward_limit(tmp_path):
    store, _, _ = make_store(tmp_path)
    controller = JobController(store, tmp_path / "project", FakeJobBackend())
    controller.path.write_text(
        json.dumps(
            {
                "dock/0/old": {
                    "key": "dock/0/old",
                    "stage": "dock",
                    "attempt": 0,
                    "run_id": "old-run",
                    "status": "submitted",
                    "command_sha256": "x",
                }
            }
        )
    )
    stage = store.verify_protocol().stage("dock")
    with pytest.raises(JobIntegrityError, match="already active"):
        controller.submit(stage, 1)
    assert controller.backend.submitted == []


def test_unsupported_scheduler_is_rejected(tmp_path):
    jobs_dir = tmp_path / "control" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "bad.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "key": "x",
                        "command": [sys.executable, "-c", "print('x')"],
                        "working_dir": "work",
                        "submit": {"scheduler": "slurm"},
                    }
                ]
            }
        )
    )
    with pytest.raises(JobError, match="scheduler must be"):
        load_job_plan(tmp_path, "bad", {}, "stage", 1)


def test_submit_exception_is_marked_uncertain(tmp_path):
    class ExplodingBackend(FakeJobBackend):
        def submit(self, request, project_dir):
            raise RuntimeError("connection lost")

    store, _, _ = make_store(tmp_path)
    controller = JobController(store, tmp_path / "project", ExplodingBackend())
    stage = store.verify_protocol().stage("dock")
    success, detail = controller.submit(stage, 1)
    assert success is False
    assert "uncertain" in detail
    jobs = json.loads(controller.path.read_text())
    assert jobs["dock/1/parent"]["status"] == "submit_uncertain"


def test_cancel_all_marks_uncertain_submission_as_error(tmp_path):
    store, _, _ = make_store(tmp_path)
    controller = JobController(store, tmp_path / "project", FakeJobBackend())
    controller.path.write_text(
        json.dumps(
            {
                "dock/1/x": {
                    "key": "dock/1/x",
                    "status": "submitting",
                    "stage": "dock",
                    "attempt": 1,
                }
            }
        )
    )
    controller.cancel_all("halt")
    assert controller.last_cancel_errors
    assert "uncertain" in controller.last_cancel_errors[0]["error"]
    assert store.cancel_errors_path.is_file()


def test_job_plan_marks_campaign_owned_runs(tmp_path):
    jobs_dir = tmp_path / "control" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "tagged.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "key": "x",
                        "command": [sys.executable, "-c", "print('x')"],
                        "working_dir": "work",
                        "submit": {"scheduler": "local", "system_tags": ["peptide"]},
                    }
                ]
            }
        )
    )
    requests = load_job_plan(tmp_path, "tagged", {}, "stage", 1)
    assert requests[0].submit["system_tags"] == ["peptide", "campaign"]


def test_job_plan_rejects_path_escape(tmp_path):
    jobs_dir = tmp_path / "control" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "bad.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "key": "x",
                        "command": ["/bin/true"],
                        "working_dir": "../escape",
                    }
                ]
            }
        )
    )
    with pytest.raises(JobError, match="without"):
        load_job_plan(tmp_path, "bad", {}, "stage", 1)


def test_job_plan_rejects_unknown_submit_field(tmp_path):
    jobs_dir = tmp_path / "control" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "bad.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "key": "x",
                        "command": ["/bin/true"],
                        "working_dir": "work",
                        "submit": {"evil": True},
                    }
                ]
            }
        )
    )
    with pytest.raises(JobError, match="unknown fields"):
        load_job_plan(tmp_path, "bad", {}, "stage", 1)


def test_compchem_status_reads_run_record(tmp_path):
    project = tmp_path / "project"
    runs = project / ".magnolia" / "runs"
    runs.mkdir(parents=True)
    run_id = "haddock3_20260101_000000_abc123"
    (runs / f"20260101_{run_id}.yaml").write_text(
        "\n".join(
            [
                f"run_id: {run_id}",
                "tool: haddock3",
                "status: pass",
                "lifecycle: fetched",
                "resources:",
                "  ncores: 4",
                "remote:",
                "  slurm:",
                "    state: COMPLETED",
                "    elapsed: '02:00:00'",
            ]
        )
    )
    backend = CompchemJobBackend()
    status = backend.status({"run_id": run_id}, project)
    assert status.terminal is True
    assert status.success is True
    assert status.core_hours == 8


def test_poller_is_invoked_once_per_poll(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend()
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    controller.poll(stage, 1)
    assert backend.poll_calls == 1


def test_poller_error_stays_pending_and_is_logged(tmp_path):
    class BrokenPoll(FakeJobBackend):
        def poll(self, project_dir):
            raise RuntimeError("poller down")

    store, _, _ = make_store(tmp_path)
    backend = BrokenPoll()
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    outcome = controller.poll(stage, 1)
    assert outcome.terminal is False
    assert "poller failed" in outcome.detail
    assert store.job_errors_path.is_file()


def test_job_wait_timeout_cancels_jobs(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend(
        statuses={
            "fake-1": [JobStatus(terminal=False, state="RUNNING")],
            "fake-2": [JobStatus(terminal=False, state="RUNNING")],
        }
    )
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    jobs = json.loads(controller.path.read_text())
    for record in jobs.values():
        record["submitted_at"] = "2020-01-01T00:00:00+00:00"
    controller.path.write_text(json.dumps(jobs))
    outcome = controller.poll(stage, 1, timeout_seconds=1)
    assert outcome.terminal is True
    assert outcome.category == "infra"
    assert len(backend.cancelled) == 2


def test_completed_jobs_are_not_cancelled_after_timeout(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend(
        statuses={
            "fake-1": [JobStatus(terminal=True, success=True, state="COMPLETED")],
            "fake-2": [JobStatus(terminal=True, success=True, state="COMPLETED")],
        }
    )
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    jobs = json.loads(controller.path.read_text())
    for record in jobs.values():
        record["submitted_at"] = "2020-01-01T00:00:00+00:00"
    controller.path.write_text(json.dumps(jobs))
    outcome = controller.poll(stage, 1, timeout_seconds=1)
    assert outcome.success is True
    assert outcome.category == "success"
    assert backend.cancelled == []


def test_timeout_preserves_current_core_hours(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend(
        statuses={
            "fake-1": [JobStatus(terminal=False, state="RUNNING", core_hours=3)],
            "fake-2": [JobStatus(terminal=False, state="RUNNING", core_hours=4)],
        }
    )
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    jobs = json.loads(controller.path.read_text())
    for record in jobs.values():
        record["submitted_at"] = "2020-01-01T00:00:00+00:00"
    controller.path.write_text(json.dumps(jobs))
    outcome = controller.poll(stage, 1, timeout_seconds=1)
    assert outcome.category == "infra"
    assert outcome.core_hours == 7
    assert len(backend.cancelled) == 2


def test_command_hash_change_is_rejected(tmp_path):
    store, _, _ = make_store(tmp_path)
    backend = FakeJobBackend()
    controller = JobController(store, tmp_path / "project", backend)
    stage = store.verify_protocol().stage("dock")
    controller.submit(stage, 1)
    plan_path = store.control_dir / "jobs" / "two-jobs.json"
    plan = json.loads(plan_path.read_text())
    plan["jobs"][0]["command"] = [sys.executable, "-c", "print('changed')"]
    plan_path.write_text(json.dumps(plan))
    with pytest.raises(JobError, match="command hash changed"):
        controller.submit(stage, 1)


def write_run(project, run_id, body):
    runs = project / ".magnolia" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"20260101_{run_id}.yaml").write_text(body, encoding="utf-8")


def test_compchem_status_local_completed_is_success(tmp_path):
    project = tmp_path / "project"
    write_run(
        project,
        "local_ok",
        "run_id: local_ok\nstatus: pass\nlifecycle: completed\n",
    )
    status = CompchemJobBackend().status({"run_id": "local_ok"}, project)
    assert status.terminal is True
    assert status.success is True
    assert status.category == "success"


def test_compchem_status_reads_nested_retry_recommendation(tmp_path):
    project = tmp_path / "project"
    write_run(
        project,
        "infra_run",
        "run_id: infra_run\nstatus: failed\nlifecycle: failed\n"
        "remote:\n  retry_recommended: true\n  retry_reason: NODE_FAIL\n",
    )
    status = CompchemJobBackend().status({"run_id": "infra_run"}, project)
    assert status.terminal is True
    assert status.success is False
    assert status.category == "infra"


def test_compchem_poller_errors_stay_pending(tmp_path):
    from compchem_tools.tools import poller

    project = tmp_path / "project"
    project.mkdir()
    original = poller.poll_jobs
    poller.poll_jobs = lambda path: {"errors": 2, "polled": 0}
    try:
        with pytest.raises(JobError, match="reported 2 error"):
            CompchemJobBackend().poll(project)
    finally:
        poller.poll_jobs = original


def test_compchem_poller_disables_automatic_retry(tmp_path):
    import os

    from compchem_tools.tools import poller

    project = tmp_path / "project"
    project.mkdir()
    observed = {}

    def fake_poll(path):
        observed["value"] = os.environ.get("MAGNOLIA_AUTO_RETRY")
        return {"errors": 0}

    original = poller.poll_jobs
    old = os.environ.pop("MAGNOLIA_AUTO_RETRY", None)
    poller.poll_jobs = fake_poll
    try:
        CompchemJobBackend().poll(project)
        assert observed["value"] == "0"
        assert "MAGNOLIA_AUTO_RETRY" not in os.environ
    finally:
        poller.poll_jobs = original
        if old is not None:
            os.environ["MAGNOLIA_AUTO_RETRY"] = old


def test_local_job_core_hours_use_submission_time(tmp_path):
    from datetime import datetime, timedelta, timezone

    project = tmp_path / "project"
    write_run(
        project,
        "local_core",
        "run_id: local_core\nstatus: pass\nlifecycle: completed\n"
        "resources:\n  ncores: 8\nremote:\n  scheduler: local\n",
    )
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    status = CompchemJobBackend().status(
        {
            "run_id": "local_core",
            "submit": {"scheduler": "local", "ncores": 8},
            "submitted_at": started,
        },
        project,
    )
    assert status.success is True
    assert 15.9 <= status.core_hours <= 16.1


def test_compchem_status_marks_retry_recommended_as_infra(tmp_path):
    project = tmp_path / "project"
    runs = project / ".magnolia" / "runs"
    runs.mkdir(parents=True)
    run_id = "run_abc"
    (runs / f"20260101_{run_id}.yaml").write_text(
        "\n".join(
            [
                f"run_id: {run_id}",
                "status: failed",
                "lifecycle: failed",
                "retry_recommended: true",
            ]
        )
    )
    status = CompchemJobBackend().status({"run_id": run_id}, project)
    assert status.terminal is True
    assert status.success is False
    assert status.category == "infra"
