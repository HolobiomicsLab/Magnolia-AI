import json
import os
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.executors import contracts as executor_contracts
from campaign_runner.executors.contracts import (
    ExecContext, LimitInfo, ResumeArtifact, RunOutcome, SessionIdentity, UsageRecord,
)
from campaign_runner.governor import AdmissionRequest, Governor, Policy, QuotaWaiting
from campaign_runner.jobs import FakeJobBackend, JobController
from campaign_runner.lease import AttemptLease
from campaign_runner.reviewers import Reviewer, ReviewResult
from campaign_runner.runner import CampaignRunner, RunnerConfig
from campaign_runner.routing import ResolvedRoute, resolve_route
from campaign_runner.state import CampaignStore, QuotaWait
from campaign_runner.workers import (
    InvocationGovernance,
    OpenCodeExecutor,
    WorkerResult,
    empty_usage,
)


NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)


def _store(
    tmp_path,
    *,
    review=False,
    repair=False,
    worker_model="provider/model",
    reviewer_model=None,
):
    source = tmp_path / "source"
    source.mkdir()
    payload = {
        "schema": 1,
        "id": "quota",
        "project": "project",
        "worker": {"model": worker_model, "agent": "campaign-worker"},
        "reviewer": {
            "enabled": review,
            "model": reviewer_model or ("other/reviewer" if review else None),
        },
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
            "on_gate_fail": "repair" if repair else "human",
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
            worker_factory=lambda model, timeout, resume_from=None,
            resume_artifact=None: worker,
            jobs=JobController(store, project, FakeJobBackend()),
            reviewer=reviewer,
            quota_governor=governor,
            clock=lambda: NOW,
        ),
    )


@dataclass
class BoundSessionStub:
    """Small client stub enforcing the same stored-binding invariant as executors."""

    client: str
    prepared: SimpleNamespace
    store: CampaignStore
    calls: list[Path]
    role: str = "worker"
    resume_artifact: ResumeArtifact | None = None

    def __post_init__(self):
        if self.resume_artifact is None:
            return
        binding = json.loads(
            (self.prepared.invocation_dir / "session-binding.json").read_text()
        )
        if binding != {
            "client": self.client,
            "session_id": self.resume_artifact.session_id,
            "role": self.role,
        }:
            raise RuntimeError("unknown or rebound stub session")

    def run(self, *args, **kwargs):
        self.calls.append(self.prepared.invocation_dir)
        if self.resume_artifact is not None:
            if self.role == "worker":
                self.store.work_dir.mkdir(parents=True, exist_ok=True)
                (self.store.work_dir / "out.md").write_text("done")
                return _result()
            return WorkerResult(
                0,
                False,
                0.1,
                "bound",
                '{"issues":[],"followups":[]}',
                "",
                0,
                empty_usage(),
                1,
                [],
                [],
                outcome=RunOutcome("success"),
            )
        self.prepared.invocation_dir.mkdir(parents=True, exist_ok=True)
        artifact = ResumeArtifact(self.client, "bound", "config", self.role, "project")
        (self.prepared.invocation_dir / "session-binding.json").write_text(
            json.dumps({"client": self.client, "session_id": "bound", "role": self.role})
        )
        result = _result(quota=True, attempts=1, text="partial")
        result.resume_artifact = artifact
        result.session_identity = SessionIdentity(
            self.client, "test", "bound", "project", self.role, "config"
        )
        return result


class BoundSessionFactory:
    def __init__(self, client, store, role="worker"):
        self.client = client
        self.store = store
        self.role = role
        self.calls = []
        self.constructed = []

    def __call__(self, resume_from=None, resume_artifact=None):
        if resume_from is None:
            invocation = (
                self.store.control_dir
                / "executors"
                / f"{self.client}-{len(self.constructed) + 1}"
            )
        else:
            invocation = Path(resume_from)
        executor = BoundSessionStub(
            self.client,
            SimpleNamespace(invocation_dir=invocation),
            self.store,
            self.calls,
            self.role,
            resume_artifact,
        )
        self.constructed.append(invocation)
        return executor


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


def _real_governor(tmp_path, **policy):
    return Governor(
        Policy(min_seconds_between_runs=1, **policy),
        ledger_path=tmp_path / "usage.jsonl",
        clock=lambda: NOW,
    )


def _shared_hold(governor):
    route = ResolvedRoute(
        "opencode", "provider/model", "model", "opencode:provider", "worker", "test"
    )
    first = governor.admit(AdmissionRequest(route, "one", None, "project", "worker"))
    first.mark_started(pid=1, pgid=1, process_start_token="one")
    first.release(outcome=RunOutcome("quota", LimitInfo("quota", None, "test")))
    return governor.quota_wait("opencode:provider").hold_id


def test_typed_daily_limit_creates_durable_checkpoint(tmp_path):
    store = _store(tmp_path)
    reset = NOW + timedelta(hours=15)
    worker = ResultWorker(
        store.run_dir,
        [
            WorkerResult(
                0,
                False,
                0.0,
                None,
                "",
                "",
                0,
                empty_usage(),
                0,
                [],
                [],
                outcome=RunOutcome(
                    "quota", LimitInfo("daily_limit", reset, "usage governor")
                ),
            )
        ],
    )

    state = _runner(store, worker, _real_governor(tmp_path / "governor")).run_once()[
        "state"
    ]

    assert state["state"] == "waiting_quota"
    assert state["attempt"] == 0
    assert state["quota_wait"]["reset_at"] == reset.isoformat()
    assert state["quota_wait"]["hold_id"]


def test_real_opencode_daily_limit_waits_without_launching(tmp_path):
    store = _store(tmp_path)
    route = ResolvedRoute(
        "opencode", "provider/model", "model", "opencode:provider", "worker", "test"
    )
    governor = _real_governor(tmp_path / "governor", max_runs_per_day=1)
    prior = governor.admit(AdmissionRequest(route, "prior", None, "project", "worker"))
    prior.mark_started(pid=1, pgid=1, process_start_token="prior")
    prior.release(outcome=RunOutcome("success"))
    marker = tmp_path / "opencode-launched"
    binary = tmp_path / "opencode"
    binary.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
    binary.chmod(0o755)
    executor = OpenCodeExecutor(
        binary=str(binary),
        model="provider/model",
        agent="campaign-worker",
        governance=InvocationGovernance(governor, route, "campaign:q", "project"),
    )

    state = _runner(store, executor, governor).run_once()["state"]

    assert state["state"] == "waiting_quota"
    assert state["attempt"] == 0
    assert state["quota_wait"]["reset_at"] == (
        NOW.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    ).isoformat()
    assert not marker.exists()


@pytest.mark.parametrize(
    "outcome_kind",
    ["turn_limit", "price_limit", "model_mismatch", "cap_reached"],
)
def test_non_review_reviewer_outcome_never_passes(tmp_path, monkeypatch, outcome_kind):
    store = _store(tmp_path, review=True)
    worker = ResultWorker(store.run_dir, [_result()])

    class InvalidReviewerExecutor:
        def run(self, *args, **kwargs):
            return WorkerResult(
                0,
                False,
                0.1,
                "review-session",
                "",
                "",
                0,
                empty_usage(),
                1,
                [],
                [],
                outcome=RunOutcome(outcome_kind),
            )

    monkeypatch.setattr(
        "campaign_runner.reviewers.executor_for",
        lambda route, role, context: InvalidReviewerExecutor(),
    )

    state = _runner(store, worker, HoldGovernor(), Reviewer(binary="unused")).run_once()[
        "state"
    ]

    assert state["state"] == "waiting_human"
    receipt = json.loads(
        (store.attempts_dir / "work" / "001" / "receipt.json").read_text()
    )
    assert receipt["status"] == "failed"


@pytest.mark.parametrize(
    ("outcome", "returncode"),
    [(RunOutcome("success"), 0), (RunOutcome("failure"), 1)],
)
def test_empty_or_failed_reviewer_never_passes(
    tmp_path, monkeypatch, outcome, returncode
):
    store = _store(tmp_path, review=True)
    worker = ResultWorker(store.run_dir, [_result()])

    class EmptyReviewerExecutor:
        def run(self, *args, **kwargs):
            return WorkerResult(
                returncode,
                False,
                0.1,
                "review-session",
                "",
                "",
                0,
                empty_usage(),
                1,
                [],
                [],
                outcome=outcome,
            )

    monkeypatch.setattr(
        "campaign_runner.reviewers.executor_for",
        lambda route, role, context: EmptyReviewerExecutor(),
    )

    state = _runner(store, worker, HoldGovernor(), Reviewer(binary="unused")).run_once()[
        "state"
    ]

    assert state["state"] == "waiting_human"
    receipt = json.loads(
        (store.attempts_dir / "work" / "001" / "receipt.json").read_text()
    )
    assert receipt["status"] == "failed"


def test_invalid_reviewer_uses_repair_budget_then_waits_human(tmp_path, monkeypatch):
    store = _store(tmp_path, review=True, repair=True)
    worker = ResultWorker(store.run_dir, [_result(), _result()])

    class CappedReviewerExecutor:
        def run(self, *args, **kwargs):
            return WorkerResult(
                0,
                False,
                0.1,
                "review-session",
                "",
                "",
                0,
                empty_usage(),
                1,
                [],
                [],
                outcome=RunOutcome("turn_limit"),
            )

    monkeypatch.setattr(
        "campaign_runner.reviewers.executor_for",
        lambda route, role, context: CappedReviewerExecutor(),
    )
    runner = _runner(store, worker, HoldGovernor(), Reviewer(binary="unused"))

    state = runner.run_once()["state"]

    assert state["state"] == "waiting_human"
    assert state["attempt"] == 2
    assert worker.calls == 2


def test_reviewer_daily_limit_waits_once(tmp_path, monkeypatch):
    store = _store(tmp_path, review=True)
    governor = _real_governor(tmp_path / "governor")
    worker = ResultWorker(store.run_dir, [_result()])
    reset = NOW + timedelta(hours=15)
    calls = 0

    class DailyLimitedReviewer:
        def run(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return WorkerResult(
                0,
                False,
                0.1,
                "review-session",
                "",
                "",
                0,
                empty_usage(),
                0,
                [],
                [],
                outcome=RunOutcome(
                    "quota", LimitInfo("daily_limit", reset, "usage governor")
                ),
            )

    monkeypatch.setattr(
        "campaign_runner.reviewers.executor_for",
        lambda route, role, context: DailyLimitedReviewer(),
    )

    state = _runner(store, worker, governor, Reviewer(binary="unused")).run_once()[
        "state"
    ]

    assert state["state"] == "waiting_quota"
    assert state["quota_wait"]["suspended_phase"] == "reviewer"
    assert state["quota_wait"]["reset_at"] == reset.isoformat()
    assert calls == 1


@pytest.mark.parametrize(
    ("limit_kind", "expected_reason"),
    [("quota", "governor quota"), ("daily_limit", "daily_limit")],
)
def test_real_opencode_reviewer_governor_limit_waits(
    tmp_path, limit_kind, expected_reason
):
    store = _store(tmp_path, review=True)
    route = resolve_route(
        "reviewer", {"reviewer": {"model": "other/reviewer"}}
    )
    policy = {"max_runs_per_day": 1} if limit_kind == "daily_limit" else {}
    governor = _real_governor(tmp_path / "governor", **policy)
    prior = governor.admit(
        AdmissionRequest(route, "prior", None, "project", "reviewer")
    )
    prior.mark_started(pid=1, pgid=1, process_start_token="prior")
    prior.release(
        outcome=(
            RunOutcome("success")
            if limit_kind == "daily_limit"
            else RunOutcome("quota", LimitInfo("quota", None, "test"))
        )
    )
    marker = tmp_path / "opencode-launched"
    binary = tmp_path / "opencode"
    binary.write_text(
        f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8"
    )
    binary.chmod(0o755)
    reviewer = Reviewer(
        binary=str(binary),
        governance=InvocationGovernance(
            governor, route, "campaign:q", "project"
        ),
    )
    worker = ResultWorker(store.run_dir, [_result()])

    state = _runner(store, worker, governor, reviewer).run_once()["state"]

    assert state["state"] == "waiting_quota"
    assert state["quota_wait"]["suspended_phase"] == "reviewer"
    assert state["quota_wait"]["reason"] == expected_reason
    assert worker.calls == 1
    assert not marker.exists()


def test_reviewer_quota_with_stop_reason_waits(tmp_path):
    store = _store(tmp_path, review=True)
    worker = ResultWorker(store.run_dir, [_result()])

    class QuotaReviewer:
        def run(self, *args, **kwargs):
            return ReviewResult(
                model="other/reviewer",
                stopped_reason="daily_limit",
                outcome=RunOutcome(
                    "quota", LimitInfo("daily_limit", NOW + timedelta(hours=1), "test")
                ),
            )

    state = _runner(store, worker, HoldGovernor(), QuotaReviewer()).run_once()["state"]

    assert state["state"] == "waiting_quota"
    assert state["quota_wait"]["suspended_phase"] == "reviewer"


def test_reviewer_pause_file_remains_stopped(tmp_path):
    store = _store(tmp_path, review=True)
    worker = ResultWorker(store.run_dir, [_result()])

    class PausingReviewer:
        def run(self, *args, **kwargs):
            (store.run_dir / "PAUSE").write_text("pause\n")
            return ReviewResult(
                model="other/reviewer",
                stopped_reason="pause requested",
                outcome=RunOutcome("cancelled"),
            )

    state = _runner(store, worker, HoldGovernor(), PausingReviewer()).run_once()["state"]
    receipt = json.loads(
        (store.attempts_dir / "work" / "001" / "receipt.json").read_text()
    )

    assert state["state"] == "paused"
    assert receipt["status"] == "stopped"


def test_unbound_worker_resume_artifact_uses_failure_path(tmp_path):
    store = _store(tmp_path)

    class UnboundWorker:
        def run(self, *args, **kwargs):
            result = _result()
            result.resume_artifact = ResumeArtifact(
                "opencode", "session", "digest", "worker", "project"
            )
            return result

    result = _runner(store, UnboundWorker(), HoldGovernor()).run_once()
    receipt = json.loads(
        (store.attempts_dir / "work" / "001" / "receipt.json").read_text()
    )

    assert result["state"]["state"] == "waiting_human"
    assert receipt["status"] == "failed"
    assert "resumable worker did not expose" in receipt["errors"][0]


def test_manual_shared_hold_release_wakes_all_campaigns(tmp_path):
    governor_dir = tmp_path / "governor"
    governor_dir.mkdir()
    governor = _real_governor(governor_dir)
    hold_id = _shared_hold(governor)
    stores = []
    for name in ("a", "b"):
        campaign_dir = tmp_path / name
        campaign_dir.mkdir()
        store = _store(campaign_dir)
        store.wait_for_quota(
            QuotaWait(
                "quota",
                "opencode:provider",
                NOW,
                hold_id,
                None,
                "pre-worker",
                "work",
                0,
            )
        )
        stores.append(store)
    first = _runner(stores[0], ResultWorker(stores[0].run_dir, [_result()]), governor)
    second = _runner(stores[1], ResultWorker(stores[1].run_dir, [_result()]), governor)

    assert first.resume_quota(manual=True).state == "queued"
    assert second.run_once()["state"]["state"] == "completed"


def test_resumed_opencode_database_usage_is_delta(tmp_path, monkeypatch):
    store = _store(tmp_path)
    cumulative_values = iter((100.0, 300.0))

    def cumulative(path, session):
        return {**empty_usage(), "input_tokens": next(cumulative_values)}

    monkeypatch.setattr("campaign_runner.runner.read_session_usage", cumulative)
    worker = ResultWorker(
        store.run_dir,
        [_result(quota=True, attempts=1, text="partial"), _result()],
    )
    runner = _runner(store, worker, HoldGovernor())
    runner.config.usage_db = "db"

    assert runner.run_once()["state"]["state"] == "waiting_quota"
    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "completed"
    assert store.load().usage["input_tokens"] == 300.0


def test_resumed_worker_prelaunch_quota_preserves_session(tmp_path):
    store = _store(tmp_path)
    calls = []

    class RecordingWorker(ResultWorker):
        def run(self, *args, **kwargs):
            calls.append(kwargs.get("resume_session"))
            return super().run(*args, **kwargs)

    worker = RecordingWorker(
        store.run_dir,
        [
            _result(quota=True, attempts=1, text="partial"),
            _result(quota=True, attempts=0, text=""),
            _result(),
        ],
    )
    runner = _runner(store, worker, HoldGovernor())

    assert runner.run_once()["state"]["state"] == "waiting_quota"
    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "waiting_quota"
    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "completed"
    assert calls == [None, "session", "session"]


def test_resumed_worker_prelaunch_quota_preserves_resume_artifact(tmp_path):
    store = _store(tmp_path)
    artifact = ResumeArtifact(
        "claude-code", "session", "config", "worker", "project"
    )
    seen = []

    @dataclass
    class ResumeAwareWorker:
        results: list[WorkerResult]
        prepared: SimpleNamespace
        resume_artifact: ResumeArtifact | None = None
        resume_permitted: bool = False

        def run(self, *args, **kwargs):
            seen.append((kwargs.get("resume_session"), self.resume_artifact))
            result = self.results.pop(0)
            if not result.outcome or result.outcome.kind != "quota":
                (store.run_dir / "work").mkdir(parents=True, exist_ok=True)
                (store.run_dir / "work" / "out.md").write_text("done")
            return result

    first = _result(quota=True, attempts=1, text="partial")
    first.resume_artifact = artifact
    invocation = store.control_dir / "executors" / "resume-aware"
    invocation.mkdir(parents=True)
    worker = ResumeAwareWorker(
        [first, _result(quota=True, attempts=0, text=""), _result()],
        SimpleNamespace(invocation_dir=invocation),
    )
    runner = _runner(store, worker, HoldGovernor())
    runner.config.worker_factory = (
        lambda model, timeout, resume_from=None, resume_artifact=None: replace(
            worker,
            prepared=SimpleNamespace(invocation_dir=resume_from or invocation),
            resume_artifact=resume_artifact,
            resume_permitted=resume_artifact is not None,
        )
    )
    runner._worker_model = lambda stage: "claude-code/sonnet"

    assert runner.run_once()["state"]["state"] == "waiting_quota"
    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "waiting_quota"
    checkpoint = store.load().quota_wait
    assert checkpoint.resume_artifact == artifact
    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "completed"
    assert seen == [(None, None), ("session", artifact), ("session", artifact)]


def test_missing_non_opencode_identity_is_recorded_unavailable(tmp_path):
    store = _store(tmp_path)
    worker = ResultWorker(store.run_dir, [_result()])
    runner = _runner(store, worker, HoldGovernor())
    runner._worker_model = lambda stage: "claude-code/sonnet"

    runner.run_once()

    record = json.loads(store.distill_queue_path.read_text().splitlines()[-1])
    assert record["session_ids"] == []
    assert record["sessions"] == []
    assert record["unavailable"] == [
        {
            "harness": "claude-code",
            "session_id": "session",
            "reason": "missing_session_identity",
        }
    ]


def test_resumed_worker_reconciles_stale_empty_lease(tmp_path):
    store = _store(tmp_path)
    worker = ResultWorker(
        store.run_dir, [_result(quota=True, attempts=1, text="partial"), _result()]
    )
    runner = _runner(store, worker, HoldGovernor())
    runner.run_once()
    runner.resume_quota(manual=True)
    path = store.leases_dir / "work-1.json"
    path.write_text(
        json.dumps(
            {
                "stage": "work",
                "attempt": 1,
                "owner_pid": 99999999,
                "owner_pgid": 99999999,
                "hostname": os.uname().nodename,
            }
        )
    )

    state = runner.run_once()["state"]

    assert state["state"] == "completed"
    assert not path.exists()


def test_resumed_worker_keeps_unknown_lease_busy(tmp_path):
    store = _store(tmp_path)
    worker = ResultWorker(
        store.run_dir, [_result(quota=True, attempts=1, text="partial"), _result()]
    )
    runner = _runner(store, worker, HoldGovernor())
    runner.run_once()
    runner.resume_quota(manual=True)
    path = store.leases_dir / "work-1.json"
    path.write_text(json.dumps({"stage": "work", "attempt": 1}))

    result = runner.run_once()

    assert result["state"]["state"] == "running"
    assert result["actions"] == ["busy: attempt lease owner is unknown: work/1"]
    assert path.exists()


@pytest.mark.parametrize("client", ["claude-code", "codex", "vibe"])
def test_bound_worker_quota_resumes_original_invocation(client, tmp_path):
    store = _store(tmp_path, worker_model=f"{client}/test")
    factory = BoundSessionFactory(client, store)
    runner = _runner(store, object(), HoldGovernor())
    runner.config.worker_factory = (
        lambda model, timeout, resume_from=None, resume_artifact=None: factory(
            resume_from, resume_artifact
        )
    )

    assert runner.run_once()["state"]["state"] == "waiting_quota"
    checkpoint = store.load().quota_wait
    assert checkpoint.resume_from == str(factory.constructed[0])
    assert Path(checkpoint.resume_from).is_relative_to(store.control_dir / "executors")

    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "completed"
    assert factory.constructed == [factory.constructed[0], factory.constructed[0]]
    assert factory.calls == [factory.constructed[0], factory.constructed[0]]


@pytest.mark.parametrize("client", ["claude-code", "codex", "vibe"])
def test_bound_reviewer_quota_resumes_original_invocation(
    client, tmp_path, monkeypatch
):
    store = _store(tmp_path, review=True, reviewer_model=f"{client}/test")
    factory = BoundSessionFactory(client, store, role="reviewer")

    def executor_for_stub(route, role, context):
        assert role == "reviewer"
        return factory(context.resume_from, context.resume_artifact)

    monkeypatch.setattr("campaign_runner.reviewers.executor_for", executor_for_stub)
    reviewer = Reviewer(
        binary="unused",
        context_factory=lambda route, cwd, attempt_dir: ExecContext(
            route=route,
            role="reviewer",
            project_dir=cwd,
            run_dir=store.run_dir,
            control_dir=store.control_dir,
            work_dir=store.work_dir,
        ),
    )
    worker = ResultWorker(store.run_dir, [_result()])
    runner = _runner(store, worker, HoldGovernor(), reviewer)

    assert runner.run_once()["state"]["state"] == "waiting_quota"
    checkpoint = store.load().quota_wait
    assert checkpoint.resume_from == str(factory.constructed[0])

    runner.resume_quota(manual=True)
    assert runner.run_once()["state"]["state"] == "completed"
    assert worker.calls == 1
    assert factory.constructed == [factory.constructed[0], factory.constructed[0]]


@pytest.mark.parametrize("tamper", ["directory", "session"])
def test_tampered_bound_worker_checkpoint_uses_failure_path(tamper, tmp_path):
    store = _store(tmp_path, worker_model="claude-code/test")
    factory = BoundSessionFactory("claude-code", store)
    runner = _runner(store, object(), HoldGovernor())
    runner.config.worker_factory = (
        lambda model, timeout, resume_from=None, resume_artifact=None: factory(
            resume_from, resume_artifact
        )
    )
    assert runner.run_once()["state"]["state"] == "waiting_quota"
    checkpoint = store.load().quota_wait
    if tamper == "directory":
        changed = replace(
            checkpoint,
            resume_from=str(store.control_dir / "executors" / "other-invocation"),
        )
    else:
        changed = replace(
            checkpoint,
            resume_artifact=replace(checkpoint.resume_artifact, session_id="forged"),
        )
    store.refresh_quota_wait(changed)

    runner.resume_quota(manual=True)
    result = runner.run_once()

    assert result["state"]["state"] == "waiting_human"
    assert len(factory.calls) == 1


def test_resumed_worker_reconciles_provably_dead_worker_group(tmp_path):
    store = _store(tmp_path)
    worker = ResultWorker(
        store.run_dir, [_result(quota=True, attempts=1, text="partial"), _result()]
    )
    runner = _runner(store, worker, HoldGovernor())
    runner.run_once()
    runner.resume_quota(manual=True)
    path = store.leases_dir / "work-1.json"
    path.write_text(json.dumps({
        "stage": "work",
        "attempt": 1,
        "owner_pid": 99999999,
        "owner_pgid": 99999999,
        "worker_pid": 99999998,
        "worker_pgid": 99999998,
        "hostname": os.uname().nodename,
    }))

    result = runner.run_once()

    assert result["state"]["state"] == "completed"
    assert not path.exists()


def test_resumed_worker_keeps_live_worker_group_busy(tmp_path):
    store = _store(tmp_path)
    worker = ResultWorker(
        store.run_dir, [_result(quota=True, attempts=1, text="partial"), _result()]
    )
    runner = _runner(store, worker, HoldGovernor())
    runner.run_once()
    runner.resume_quota(manual=True)
    path = store.leases_dir / "work-1.json"
    path.write_text(json.dumps({
        "stage": "work",
        "attempt": 1,
        "owner_pid": 99999999,
        "owner_pgid": 99999999,
        "worker_pid": os.getpid(),
        "worker_pgid": os.getpgid(0),
        "hostname": os.uname().nodename,
    }))

    result = runner.run_once()

    assert result["state"]["state"] == "running"
    assert result["actions"] == ["busy: attempt worker is still active: work/1"]
    assert path.exists()


def test_attempt_lease_records_worker_start_token(tmp_path):
    lease = AttemptLease.acquire(tmp_path, "work", 1)
    try:
        lease.update_pid(os.getpid(), os.getpgid(0))
        recorded = json.loads(lease.path.read_text())
        assert recorded["worker_start_token"]
    finally:
        lease.release()


def test_checkpoint_limit_does_not_mutate_limit_info(tmp_path):
    governor = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage.jsonl",
        clock=lambda: NOW,
    )
    limit = LimitInfo("daily_limit", None, "client")

    waiting = governor.checkpoint_limit("claude-code", limit)

    assert waiting.reset_at is not None
    assert waiting.hold_id is not None
    assert limit == LimitInfo("daily_limit", None, "client")


def test_reviewer_cancellation_without_stop_file_is_stopped(tmp_path):
    store = _store(tmp_path, review=True)
    worker = ResultWorker(store.run_dir, [_result()])

    class CancelledReviewer:
        def run(self, *args, **kwargs):
            return ReviewResult(
                model="other/reviewer",
                stopped_reason="observation failed",
                outcome=RunOutcome("cancelled"),
            )

    result = _runner(store, worker, HoldGovernor(), CancelledReviewer()).run_once()
    receipt = json.loads(
        (store.attempts_dir / "work" / "001" / "receipt.json").read_text()
    )

    assert result["state"]["state"] == "halted"
    assert receipt["status"] == "stopped"


@pytest.mark.parametrize("client", ["claude-code", "codex", "vibe"])
def test_pre_client_binding_without_client_key_matches_own_client(client):
    expected = {
        "client": client,
        "configuration_identity_digest": "digest",
        "role": "worker",
        "project_identity": "project",
    }
    legacy = {key: value for key, value in expected.items() if key != "client"}

    assert executor_contracts.binding_identity_matches(legacy, expected)
    assert not executor_contracts.binding_identity_matches(
        {**legacy, "role": "reviewer"}, expected
    )
