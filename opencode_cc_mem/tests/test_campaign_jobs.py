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
