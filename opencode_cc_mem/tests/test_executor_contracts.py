from datetime import datetime, timezone

import pytest

from campaign_runner.executors.contracts import (
    ExecContext,
    RunOutcome,
    SessionIdentity,
    UsageRecord,
)
from campaign_runner.reviewers import ReviewResult
from campaign_runner.workers import WorkerResult


def test_limit_outcome_preserves_partial_result():
    outcome = RunOutcome(kind="quota")
    outcome.limit = outcome.limit.__class__(
        reason="subscription exhausted",
        reset_at=datetime(2026, 11, 16, tzinfo=timezone.utc),
        evidence_source="client stderr",
    )
    worker = WorkerResult(0, False, 1.0, "s1", "partial", "partial", 0, {}, 1, [], [])
    worker.outcome = outcome
    worker_usage = {"partial": True}
    worker.usage = worker_usage
    review = ReviewResult(raw_text="partial", outcome=outcome)

    assert worker.final_text == "partial"
    assert worker.outcome.limit.reason == "subscription exhausted"
    assert worker.usage is worker_usage
    assert review.outcome.kind == "quota"
    assert outcome.overrides_repair


def test_usage_unknown_cache_and_resume_delta():
    raw = {
        "schema_version": "opencode-usage-v1",
        "tokens": {
            "input": 100,
            "output": 20,
            "reasoning": 3,
            "cache": {"read": 40, "write": 5},
        },
        "cost": 0.1,
    }
    identity = SessionIdentity("opencode", "provider/model", "session", "project", "worker", "policy")
    first = UsageRecord.from_raw(raw, session_identity=identity)
    resumed = UsageRecord.from_raw(
        {**raw, "tokens": {"input": 150, "output": 30, "reasoning": 4, "cache": {"read": 70}}},
        baseline=first,
        session_identity=identity,
    )
    unknown = UsageRecord.from_raw({"schema_version": "opencode-usage-v1", "tokens": {"output": 2}})

    assert first.input_tokens == 100
    assert first.cached_tokens == 40
    assert first.cache_write_tokens == 5
    assert resumed.input_tokens == 50
    assert resumed.cached_tokens == 30
    assert resumed.cache_write_tokens is None
    assert unknown.input_tokens is None
    assert unknown.cached_tokens is None
    assert unknown.completeness == "partial"


def test_exec_context_and_session_identity_defaults(tmp_path):
    context = ExecContext(route=None, role="worker")
    identity = SessionIdentity("claude-code", "gpt-5", "s", "p", "reviewer", "policy")
    assert context.project_dir is None
    assert context.run_dir is None
    assert identity.hook_harness == "claude"
    with pytest.raises(ValueError):
        RunOutcome(kind="unknown")
