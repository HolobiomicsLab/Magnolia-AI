import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.jobs import FakeJobBackend, JobController, JobStatus  # noqa: E402
from campaign_runner.lease import AttemptLease  # noqa: E402
from campaign_runner.reviewers import ReviewResult  # noqa: E402
from campaign_runner.runner import CampaignRunner, RunnerConfig  # noqa: E402
from campaign_runner.state import CampaignStore  # noqa: E402
from campaign_runner.workers import WorkerResult, empty_usage  # noqa: E402


def protocol_payload(
    max_tokens=1000,
    retry="repair_once",
    prepare_gate="raise SystemExit(0)",
    hard_analyze=False,
    max_cost_usd=None,
):
    payload = {
        "schema": 1,
        "id": "runner-pilot",
        "project": "project",
        "worker": {
            "model": "opencode-go/deepseek-v4.1-flash",
            "hard_stage_model": "kimi-code-plan-cn/k3.1-pro#high",
            "agent": "campaign-worker",
        },
        "reviewer": {
            "enabled": True,
            "model": "opencode-go/deepseek-v4.1-flash",
            "alternate_model": "zai-coding-plan/glm-5.3",
        },
        "budget": {
            "max_tokens": max_tokens,
            "max_wall_hours": 1,
            "max_core_hours": 10,
            "max_attempts_per_stage": 2,
            "max_parallel_jobs": 2,
            "max_followups": 0,
        },
        "stages": [
            {"id": "confirm", "type": "human"},
            {
                "id": "prepare",
                "type": "script",
                "command": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('work/prepared.txt').write_text('ready')",
                ],
                "outputs": ["work/prepared.txt"],
                "gate": [sys.executable, "-c", prepare_gate],
                "retry": retry,
                "on_gate_fail": "repair" if retry == "repair_once" else "human",
            },
            {
                "id": "analyze",
                "type": "opencode",
                "model": "kimi-code-plan-cn/k3.1-pro#high",
                "prompt": "prompts/analyze.md",
                "outputs": ["work/analysis.md"],
                "gate": [sys.executable, "-c", "raise SystemExit(0)"],
                "review": True,
                "retry": retry,
                "on_gate_fail": "repair" if retry == "repair_once" else "human",
            },
            {
                "id": "dock",
                "type": "job",
                "command": "dummy",
                "gate": [sys.executable, "-c", "raise SystemExit(0)"],
                "retry": "infra_once",
                "on_gate_fail": "human",
            },
            {"id": "final", "type": "human"},
        ],
    }
    if hard_analyze:
        for stage in payload["stages"]:
            if stage["id"] == "analyze":
                stage.pop("model")
                stage["hard"] = True
    if max_cost_usd is not None:
        payload["budget"]["max_cost_usd"] = max_cost_usd
    return payload


def initialize(
    tmp_path,
    max_tokens=1000,
    retry="repair_once",
    prepare_gate="raise SystemExit(0)",
    hard_analyze=False,
    max_cost_usd=None,
):
    source = tmp_path / "campaign"
    source.mkdir()
    protocol = source / "protocol.json"
    protocol.write_text(
        json.dumps(
            protocol_payload(
                max_tokens,
                retry,
                prepare_gate,
                hard_analyze,
                max_cost_usd,
            )
        )
    )
    brief = source / "brief.md"
    brief.write_text("brief")
    prompts = source / "prompts"
    prompts.mkdir()
    (prompts / "analyze.md").write_text("Analyze {run_dir}")
    store = CampaignStore(tmp_path / "project" / "runs" / "runner")
    store.initialize(protocol, brief)
    jobs = store.control_dir / "jobs"
    jobs.mkdir()
    (jobs / "dummy.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "key": "dock",
                        "command": [sys.executable, "-c", "print('dock')"],
                        "working_dir": "work/job",
                        "submit": {"scheduler": "local"},
                    }
                ]
            }
        )
    )
    return store


class FakeWorker:
    def __init__(self, run_dir, gate_fail=False):
        self.run_dir = run_dir
        self.gate_fail = gate_fail
        self.calls = []

    def run(
        self,
        prompt,
        cwd,
        attempt_dir,
        stem="worker",
        resume_session=None,
        retries=None,
        control=None,
    ):
        self.calls.append(prompt)
        (self.run_dir / "work").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "work" / "analysis.md").write_text("analysis")
        return WorkerResult(
            returncode=0,
            timed_out=False,
            duration_seconds=0.1,
            session_id="ses_fake",
            final_text="done",
            all_text="done",
            tool_calls=0,
            usage=empty_usage(),
            attempts=1,
            stdout_paths=[],
            stderr_paths=[],
        )


class SleepWorker:
    def __init__(self):
        self.started = False

    def run(
        self,
        prompt,
        cwd,
        attempt_dir,
        stem="worker",
        resume_session=None,
        retries=None,
        control=None,
    ):
        self.started = True
        deadline = 10
        while deadline > 0:
            reason = control.should_stop() if control else None
            if reason:
                return WorkerResult(
                    returncode=-15,
                    timed_out=False,
                    duration_seconds=0.1,
                    session_id="ses_sleep",
                    final_text="",
                    all_text="",
                    tool_calls=0,
                    usage=empty_usage(),
                    attempts=1,
                    stdout_paths=[],
                    stderr_paths=[],
                    stopped_reason=reason,
                )
            time.sleep(0.05)
            deadline -= 0.05
        return WorkerResult(
            returncode=0,
            timed_out=False,
            duration_seconds=1,
            session_id="ses_sleep",
            final_text="late",
            all_text="late",
            tool_calls=0,
            usage=empty_usage(),
            attempts=1,
            stdout_paths=[],
            stderr_paths=[],
        )


class FakeReviewer:
    def __init__(self, issues=None, followups=None):
        self.issues = issues or []
        self.followups = followups or []
        self.calls = []

    def run(self, model, prompt, cwd, attempt_dir, run_dir, control=None):
        self.calls.append((model, prompt))
        return ReviewResult(
            issues=self.issues,
            followups=self.followups,
            raw_text="{}",
            session_id="ses_review",
            usage=empty_usage(),
            attempts=1,
            model=model,
        )


def make_runner(store, fake_worker, fake_reviewer, statuses=None):
    project = store.run_dir.parent.parent
    backend = FakeJobBackend(statuses=statuses)
    jobs = JobController(store, project, backend)
    runner = CampaignRunner(
        store,
        project,
        RunnerConfig(
            worker_factory=lambda model, timeout: fake_worker,
            jobs=jobs,
            reviewer=fake_reviewer,
        ),
    )
    runner.fake_backend = backend
    return runner


def test_hard_stage_uses_hard_model(tmp_path):
    store = initialize(tmp_path, hard_analyze=True)
    store.approve_start("tao", "start")
    fake_worker = FakeWorker(store.run_dir)
    fake_reviewer = FakeReviewer()
    models: list[str] = []
    project = store.run_dir.parent.parent
    runner = CampaignRunner(
        store,
        project,
        RunnerConfig(
            worker_factory=lambda model, timeout: (
                models.append(model) or fake_worker
            ),
            jobs=JobController(store, project, FakeJobBackend()),
            reviewer=fake_reviewer,
        ),
    )
    runner.run_once()
    store.approve("tao", "start")
    runner.run_once()
    assert models == ["kimi-code-plan-cn/k3.1-pro#high"]
    assert fake_reviewer.calls[0][0] == "opencode-go/deepseek-v4.1-flash"


def test_startup_approval_is_required_before_any_stage(tmp_path):
    store = initialize(tmp_path)
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    result = runner.run_once()
    assert result["state"]["state"] == "queued"
    assert result["actions"] == ["awaiting startup approval"]
    assert (store.control_dir / "requests" / "start.md").is_file()
    assert list(store.attempts_dir.iterdir()) == []


def test_project_mismatch_is_rejected(tmp_path):
    store = initialize(tmp_path)
    wrong_project = tmp_path / "wrong"
    wrong_project.mkdir()
    with pytest.raises(Exception, match="does not match"):
        CampaignRunner(
            store,
            wrong_project,
            RunnerConfig(
                worker_factory=lambda model, timeout: FakeWorker(store.run_dir),
                jobs=JobController(store, wrong_project, FakeJobBackend()),
                reviewer=FakeReviewer(),
            ),
        )


def test_full_campaign_with_human_gates(tmp_path):
    store = initialize(tmp_path)
    store.approve_start("tao", "start")
    fake_worker = FakeWorker(store.run_dir)
    fake_reviewer = FakeReviewer()
    runner = make_runner(
        store,
        fake_worker,
        fake_reviewer,
        statuses={"fake-1": [JobStatus(terminal=False, state="RUNNING")]},
    )
    first = runner.run_once()
    assert first["state"]["state"] == "waiting_human"
    assert first["state"]["stage"] == "confirm"
    store.approve("tao", "start")
    second = runner.run_once()
    assert second["state"]["state"] == "waiting_job"
    assert second["state"]["stage"] == "dock"
    assert (store.run_dir / "work" / "prepared.txt").is_file()
    assert fake_worker.calls
    assert fake_reviewer.calls
    assert fake_reviewer.calls[0][0] == "opencode-go/deepseek-v4.1-flash"
    third = runner.run_once()
    assert third["state"]["state"] == "waiting_human"
    assert third["state"]["stage"] == "final"
    final = store.approve("tao", "done")
    assert final.state == "completed"
    receipts = list(store.attempts_dir.rglob("receipt.json"))
    assert receipts
    analysis_receipt = json.loads(
        (store.attempts_dir / "analyze" / "001" / "receipt.json").read_text()
    )
    assert analysis_receipt["session_id"] == "ses_fake"
    assert analysis_receipt["usage"]["input_tokens"] == 0
    dock_receipt = json.loads(
        (store.attempts_dir / "dock" / "001" / "receipt.json").read_text()
    )
    assert dock_receipt["run_ids"] == ["fake-1"]
    assert dock_receipt["command_hashes"]
    assert dock_receipt["test_double"] is True


def test_job_core_hours_are_counted_once_per_attempt(tmp_path):
    store = initialize(tmp_path)
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    state = store.load()
    runner._record_job_usage(
        state,
        JobStatus(terminal=False, state="RUNNING", core_hours=8),
    )
    assert store.load().usage["core_hours"] == 8
    runner._record_job_usage(
        state,
        JobStatus(terminal=False, state="RUNNING", core_hours=8),
    )
    assert store.load().usage["core_hours"] == 8
    runner._record_job_usage(
        state,
        JobStatus(terminal=True, success=True, core_hours=10),
    )
    assert store.load().usage["core_hours"] == 10


def test_reviewer_blocker_stops_for_human(tmp_path):
    store = initialize(tmp_path)
    store.approve_start("tao", "start")
    evidence = store.run_dir / "work" / "analysis.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("analysis")
    blocking = [
        {
            "severity": "blocker",
            "claim": "overclaim",
            "evidence": ["work/analysis.md"],
            "suggested_action": "fix",
        }
    ]
    fake_worker = FakeWorker(store.run_dir)
    runner = make_runner(store, fake_worker, FakeReviewer(blocking))
    runner.run_once()
    store.approve("tao", "start")
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_human"
    assert result["state"]["stage"] == "analyze"


def test_reviewer_followup_limit_is_enforced(tmp_path):
    store = initialize(tmp_path)
    store.approve_start("tao", "start")
    followup = {
        "observation": "needs a control",
        "proposed_test": "run the control",
        "estimated_core_hours": 1,
        "reason": "disambiguate",
    }
    runner = make_runner(
        store,
        FakeWorker(store.run_dir),
        FakeReviewer(followups=[followup]),
    )
    runner.run_once()
    store.approve("tao", "start")
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_human"
    receipt = json.loads(
        (store.attempts_dir / "analyze" / "001" / "receipt.json").read_text()
    )
    assert any("followups" in error for error in receipt["errors"])


def test_gate_failure_repairs_once_then_waits(tmp_path):
    store = initialize(tmp_path, prepare_gate="raise SystemExit(4)")
    store.approve_start("tao", "start")
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    runner.run_once()
    store.approve("tao", "start")
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_human"
    assert result["state"]["stage"] == "prepare"
    assert result["state"]["attempt"] == 2
    receipts = list((store.attempts_dir / "prepare").rglob("receipt.json"))
    assert len(receipts) == 2


def test_budget_exceeded_halts(tmp_path):
    store = initialize(tmp_path, max_tokens=1)
    store.approve_start("tao", "start")
    store.update_usage({"input_tokens": 2}, "test usage")
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    result = runner.run_once()
    assert result["state"]["state"] == "halted"


def test_cost_budget_exceeded_halts(tmp_path):
    store = initialize(tmp_path, max_cost_usd=1)
    store.approve_start("tao", "start")
    store.update_usage({"cost_usd": 2}, "test usage")
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    result = runner.run_once()
    assert result["state"]["state"] == "halted"


def test_zero_cost_budget_is_disabled(tmp_path):
    store = initialize(tmp_path, max_cost_usd=0)
    store.approve_start("tao", "start")
    store.update_usage({"cost_usd": 2}, "subscription cost")
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_human"


def test_validating_requires_attempt_lease(tmp_path):
    store = initialize(tmp_path)
    store.transition("running", "test", stage="analyze", attempt=1)
    store.transition("validating", "test")
    lease = AttemptLease.acquire(store.run_dir, "analyze", 1)
    try:
        runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
        result = runner.run_once()
    finally:
        lease.release()
    assert result["state"]["state"] == "validating"
    assert result["actions"] and result["actions"][0].startswith("busy:")


def test_stale_validation_lease_requires_human(tmp_path):
    store = initialize(tmp_path)
    store.transition("running", "test", stage="analyze", attempt=1)
    store.transition("validating", "test")
    path = store.leases_dir / "analyze-1.json"
    path.write_text(
        json.dumps(
            {
                "stage": "analyze",
                "attempt": 1,
                "owner_pid": 99999999,
                "owner_pgid": 99999999,
                "hostname": os.uname().nodename,
            }
        )
    )
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_human"
    assert "stale lease" in result["actions"][0]


def test_interrupted_running_state_waits_for_human(tmp_path):
    store = initialize(tmp_path)
    store.transition("running", "simulated crash")
    runner = make_runner(store, FakeWorker(store.run_dir), FakeReviewer())
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_human"


def test_job_infrastructure_failure_retries_once(tmp_path):
    store = initialize(tmp_path)
    store.approve_start("tao", "start")
    fake_worker = FakeWorker(store.run_dir)
    statuses = {
        "fake-1": [
            JobStatus(terminal=True, success=False, category="infra", state="FAILED")
        ],
        "fake-2": [JobStatus(terminal=True, success=True, state="COMPLETED")],
    }
    runner = make_runner(store, fake_worker, FakeReviewer(), statuses=statuses)
    runner.run_once()
    store.approve("tao", "start")
    runner.run_once()
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_human"
    assert result["state"]["stage"] == "final"
    dock_receipts = list((store.attempts_dir / "dock").rglob("receipt.json"))
    assert len(dock_receipts) == 2


def test_halt_interrupts_active_worker(tmp_path):
    store = initialize(tmp_path)
    store.approve_start("tao", "start")
    worker = SleepWorker()
    runner = make_runner(store, worker, FakeReviewer())
    runner.run_once()
    store.approve("tao", "start")
    result: dict = {}

    def execute():
        result.update(runner.run_once())

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    while not worker.started and time.monotonic() < deadline:
        time.sleep(0.02)
    (store.run_dir / "HALT").write_text("stop")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["state"]["state"] == "halted"
    receipt = store.attempts_dir / "analyze" / "001" / "receipt.json"
    assert receipt.is_file()
    assert json.loads(receipt.read_text())["status"] == "stopped"


def test_pause_resume_reuses_waiting_job_attempt(tmp_path):
    store = initialize(tmp_path)
    store.approve_start("tao", "start")
    runner = make_runner(
        store,
        FakeWorker(store.run_dir),
        FakeReviewer(),
        statuses={
            "fake-1": [
                JobStatus(terminal=False, state="RUNNING"),
                JobStatus(terminal=False, state="RUNNING"),
            ]
        },
    )
    runner.run_once()
    store.approve("tao", "start")
    submitted = runner.run_once()
    assert submitted["state"]["state"] == "waiting_job"
    assert submitted["state"]["attempt"] == 1
    store.pause("hold")
    paused = store.load()
    assert paused.paused_from == "waiting_job"
    resumed = store.resume("continue")
    assert resumed.state == "waiting_job"
    assert resumed.attempt == 1
    result = runner.run_once()
    assert result["state"]["state"] == "waiting_job"
    assert result["state"]["attempt"] == 1
    assert len(runner.fake_backend.submitted) == 1


def test_halt_file_cancels_active_jobs(tmp_path):
    store = initialize(tmp_path)
    store.approve_start("tao", "start")
    runner = make_runner(
        store,
        FakeWorker(store.run_dir),
        FakeReviewer(),
        statuses={"fake-1": [JobStatus(terminal=False, state="RUNNING")]},
    )
    runner.run_once()
    store.approve("tao", "start")
    running = runner.run_once()
    assert running["state"]["state"] == "waiting_job"
    (store.run_dir / "HALT").write_text("stop")
    result = runner.run_once()
    assert result["state"]["state"] == "halted"
    assert runner.fake_backend.cancelled == ["fake-1"]
