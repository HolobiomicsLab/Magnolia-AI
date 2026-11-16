from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.governor import Governor, Policy
from campaign_runner.routing import resolve_route
from router_runner import agentd
from router_runner.evaluator import GenerativeEvaluator
from router_runner.router import Router


CAPABILITIES = {"claude-code": {"evaluator": {"accepted": True}}}


class DecisionExecutor:
    def __init__(self):
        self.calls = 0

    def run(self, prompt, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            final_text=(
                '{"action":"accept","project":"proj_a","confidence":0.95,'
                '"reason":"clear"}'
            ),
            outcome=RunOutcome("success"),
            usage={},
            attempts=1,
            duration_seconds=0.01,
            session_identity=None,
        )


class QuotaExecutor(DecisionExecutor):
    def run(self, prompt, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            final_text="partial output must not be parsed",
            outcome=RunOutcome(
                "quota", LimitInfo("weekly limit", None, "fixture stream")
            ),
            usage={},
            attempts=1,
            duration_seconds=0.01,
            session_identity=None,
        )


def _governor(tmp_path):
    return Governor(
        Policy(max_concurrent=1, max_runs_per_day=50, max_turns_per_run=1,
               min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=lambda: datetime(2026, 11, 16, tzinfo=timezone.utc),
    )


def _router(tmp_path, monkeypatch, governor, executor, db_name="router.db"):
    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    monkeypatch.setattr(evaluator_module, "executor_for", lambda *args, **kwargs: executor)
    engine = agentd.get_engine(str(tmp_path / db_name), out_root=tmp_path / "runs")
    evaluator = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        governor=governor,
        project_dir=tmp_path,
        role_capabilities=CAPABILITIES,
    )
    return Router(
        engine,
        registry={"proj_a": {"inbox": str(tmp_path / "proj_a" / "inbox")}},
        evaluator=evaluator,
    )


def _unknown_hold(governor):
    route = resolve_route("evaluator", {"model": "claude-code/claude-haiku-5-5"})
    return governor.checkpoint_limit(
        route.subscription_key,
        LimitInfo("weekly limit", None, "fixture"),
    )


def test_evaluator_quota_visible_without_fallback_or_retry(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = QuotaExecutor()
    router = _router(tmp_path, monkeypatch, governor, executor)

    reply = router.say("analyze the latest result")

    assert reply.kind == "waiting_quota"
    assert reply.waiter_id
    assert executor.calls == 1
    assert router.engine.store.all("eval_decisions") == []
    assert not (tmp_path / "proj_a" / "inbox").exists()
    assert "analyze the latest result" not in (tmp_path / "usage-state.json").read_text()
    stored = router.engine.store.get("eval_waiters", "waiter_id", reply.waiter_id)
    assert stored["text"] == "analyze the latest result"


def test_evaluator_unknown_reset_waits_for_manual_resume(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    router = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = router.say("analyze the latest result").waiter_id

    still_waiting = router.resume_evaluation(waiter_id)
    assert still_waiting.kind == "waiting_quota"
    assert executor.calls == 0

    resumed = router.resume_evaluation(waiter_id, manual=True)
    assert resumed.kind == "handoff"
    assert executor.calls == 1


def test_evaluator_resume_once_after_restart(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    first = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = first.say("analyze the latest result").waiter_id
    first.engine.store.close()

    restarted = _router(tmp_path, monkeypatch, governor, executor)
    resumed = restarted.resume_evaluation(waiter_id, manual=True)
    duplicate = restarted.resume_evaluation(waiter_id, manual=True)

    assert resumed.kind == "handoff"
    assert duplicate.kind == "unavailable"
    assert executor.calls == 1
    assert len(restarted.engine.store.all("eval_decisions")) == 1
