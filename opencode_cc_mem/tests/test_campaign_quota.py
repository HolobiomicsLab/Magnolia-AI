import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.executors.contracts import (
    LimitInfo, RunOutcome, SessionIdentity, UsageRecord,
)
from campaign_runner.governor import AdmissionRequest, Governor, Policy, QuotaWaiting
from campaign_runner.jobs import FakeJobBackend, JobController
from campaign_runner.reviewers import ReviewResult
from campaign_runner.runner import CampaignRunner, RunnerConfig
from campaign_runner.routing import ResolvedRoute
from campaign_runner.state import CampaignStore, QuotaWait
from campaign_runner.workers import WorkerResult, empty_usage


NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)


def _store(tmp_path, *, review=False):
    source = tmp_path / "source"
    source.mkdir()
    payload = {
        "schema": 1,
        "id": "quota",
        "project": "project",
        "worker": {"model": "provider/model", "agent": "campaign-worker"},
        "reviewer": {"enabled": review, "model": "other/reviewer" if review else None},
        "budget": {
            "max_tokens": 1000,
            "max_wall_hours": 1,
            "max_core_hours": 1,
            "max_attempts_per_stage": 2,
            "max_parallel_jobs": 1,
            "max_followups": 0,
        },
        "stages": [{
            "id": "work", "type": "opencode", "prompt": "prompts/prompt.md",
            "outputs": ["work/out.md"],
            "gate": [sys.executable, "-c", "raise SystemExit(0)"],
            "review": review,
        }],
    }
    protocol = source / "protocol.json"
    protocol.write_text(json.dumps(payload))
    brief = source / "brief.md"
    brief.write_text("brief")
    (source / "prompts").mkdir()
    (source / "prompts" / "prompt.md").write_text("work")
    store = CampaignStore(tmp_path / "project" / "runs" / "quota")
    store.initialize(protocol, brief)
    store.approve_start("test", "start")
    return store


class HoldGovernor:
    def __init__(self, reset_at=None, hold_id="hold-1"):
        self.waiting = QuotaWaiting("opencode:provider", "call", "quota", reset_at, hold_id)
        self.resumed = []

    def quota_wait(self, key):
        return self.waiting

    def resume_key(self, key, expected_hold=None):
        self.resumed.append((key, expected_hold))
        return expected_hold == self.waiting.hold_id


class ResultWorker:
    def __init__(self, run_dir, results):
        self.run_dir = run_dir
        self.results = list(results)
        self.calls = 0

    def run(self, *args, **kwargs):
        self.calls += 1
        result = self.results.pop(0)
        if not result.outcome or result.outcome.kind != "quota":
            (self.run_dir / "work").mkdir(parents=True, exist_ok=True)
            (self.run_dir / "work" / "out.md").write_text("done")
        return result


def _result(*, quota=False, attempts=1, returncode=0, text="done", reset_at=None):
    return WorkerResult(
        returncode, False, 0.1, "session", text, text, 0, empty_usage(), attempts,
        [], [], outcome=(
            RunOutcome("quota", LimitInfo("quota", reset_at, "test")) if quota else RunOutcome("success")
        ),
    )


def _runner(store, worker, governor, reviewer=None):
    project = store.run_dir.parent.parent
    return CampaignRunner(
        store,
        project,
        RunnerConfig(
            worker_factory=lambda model, timeout: worker,
            jobs=JobController(store, project, FakeJobBackend()),
            reviewer=reviewer,
            quota_governor=governor,
            clock=lambda: NOW,
        ),
    )


def test_daily_cap_wait_starts_no_client_or_attempt(tmp_path):
    store = _store(tmp_path)
    governor = HoldGovernor(NOW + timedelta(days=1))
    worker = ResultWorker(store.run_dir, [_result(quota=True, attempts=0, text="")])
    state = _runner(store, worker, governor).run_once()["state"]
    assert state["state"] == "waiting_quota"
    assert state["attempt"] == 0


@pytest.mark.parametrize("returncode,text", [(0, ""), (1, "partial")])
def test_worker_limit_stops_all_repairs(tmp_path, returncode, text):
    store = _store(tmp_path)
    governor = HoldGovernor()
    worker = ResultWorker(
        store.run_dir, [_result(quota=True, attempts=1, returncode=returncode, text=text)]
    )
    state = _runner(store, worker, governor).run_once()["state"]
    assert state["state"] == "waiting_quota"
    assert worker.calls == 1


def test_reviewer_limit_resumes_validation_worker_called_once(tmp_path):
    store = _store(tmp_path, review=True)
    governor = HoldGovernor()
    worker = ResultWorker(store.run_dir, [_result()])

    class QuotaReviewer:
        def __init__(self):
            self.calls = 0
        def run(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ReviewResult(
                    session_id="review", model="other/reviewer",
                    outcome=RunOutcome("quota", LimitInfo("quota", None, "test")),
                )
            return ReviewResult(model="other/reviewer")

    reviewer = QuotaReviewer()
    runner = _runner(store, worker, governor, reviewer)
    assert runner.run_once()["state"]["state"] == "waiting_quota"
    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "completed"
    assert worker.calls == 1
    assert reviewer.calls == 2


def test_restart_reconcile_known_reset_auto_wake(tmp_path):
    store = _store(tmp_path)
    store.wait_for_quota(QuotaWait(
        "daily_limit", "opencode:provider", NOW - timedelta(hours=1), "hold-1",
        NOW - timedelta(seconds=1), "pre-worker", "work", 0,
    ))
    worker = ResultWorker(store.run_dir, [_result()])
    state = _runner(store, worker, HoldGovernor(NOW - timedelta(seconds=1))).run_once()["state"]
    assert state["state"] == "completed"


def test_unknown_reset_requires_manual_resume(tmp_path):
    store = _store(tmp_path)
    store.wait_for_quota(QuotaWait(
        "quota", "opencode:provider", NOW, "hold-1", None, "pre-worker", "work", 0,
    ))
    worker = ResultWorker(store.run_dir, [_result()])
    runner = _runner(store, worker, HoldGovernor())
    assert runner.run_once()["state"]["state"] == "waiting_quota"
    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "completed"


def test_waiting_pause_halt_stop(tmp_path):
    store = _store(tmp_path)
    store.wait_for_quota(QuotaWait(
        "quota", "opencode:provider", NOW, "hold-1", None, "pre-worker", "work", 0,
    ))
    worker = ResultWorker(store.run_dir, [])
    runner = _runner(store, worker, HoldGovernor())
    (store.run_dir / "PAUSE").write_text("pause\n")
    assert runner.run_once()["state"]["state"] == "paused"
    state, errors = runner.request_stop("stop")
    assert errors == []
    assert state.state == "halted"
    assert state.quota_wait is None


def test_quota_wait_exposes_hold_generation(tmp_path):
    route = ResolvedRoute(
        "opencode", "provider/model", "model", "opencode:provider", "worker", "test"
    )
    governor = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage.jsonl",
        clock=lambda: NOW,
    )
    first = governor.admit(AdmissionRequest(route, "one", None, "project", "worker"))
    first.mark_started(pid=1, pgid=1, process_start_token="one")
    first.release(outcome=RunOutcome("quota", LimitInfo("quota", None, "test")))
    waiting = governor.admit(AdmissionRequest(route, "two", None, "project", "worker"))
    assert isinstance(waiting, QuotaWaiting)
    assert waiting.hold_id
    assert waiting.hold_id == governor.quota_wait("opencode:provider").hold_id


def test_new_session_identity_reaches_ledger_receipt(tmp_path):
    route = ResolvedRoute(
        "claude-code", "sonnet", "sonnet", "claude-code", "worker", "test"
    )
    governor = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage.jsonl",
        clock=lambda: NOW,
    )
    admission = governor.admit(
        AdmissionRequest(route, "one", None, "project", "worker")
    )
    admission.mark_started(pid=1, pgid=1, process_start_token="one")
    identity = SessionIdentity(
        "claude-code", "sonnet", "new-session", "project", "worker", "policy"
    )
    admission.release(
        usage=UsageRecord(session_identity=identity), outcome=RunOutcome("success")
    )
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["session_identity"]["session_id"] == "new-session"
