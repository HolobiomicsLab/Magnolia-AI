from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.governor import Governor, Policy
from campaign_runner.process import ProcessOutcome
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


_DEFAULT_PROJECT_DIR = object()


def _router(
    tmp_path,
    monkeypatch,
    governor,
    executor,
    db_name="router.db",
    project_dir=_DEFAULT_PROJECT_DIR,
):
    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    monkeypatch.setattr(evaluator_module, "executor_for", lambda *args, **kwargs: executor)
    engine = agentd.get_engine(str(tmp_path / db_name), out_root=tmp_path / "runs")
    evaluator = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        governor=governor,
        project_dir=tmp_path if project_dir is _DEFAULT_PROJECT_DIR else project_dir,
        role_capabilities=CAPABILITIES,
    )
    return Router(
        engine,
        registry={
            "proj_a": {"inbox": str(tmp_path / "proj_a" / "inbox")},
            "proj_b": {"inbox": str(tmp_path / "proj_b" / "inbox")},
        },
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
    assert "analyze the latest result" not in (tmp_path / "usage-ledger.jsonl").read_text()
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
    assert first.say("in proj_b: unrelated work").kind == "handoff"
    waiter_id = first.say("analyze the latest result").waiter_id
    first.engine.store.close()

    restarted = _router(tmp_path, monkeypatch, governor, executor)
    resumed = restarted.resume_evaluation(waiter_id, manual=True)
    duplicate = restarted.resume_evaluation(waiter_id, manual=True)

    assert resumed.kind == "handoff"
    assert duplicate.kind == "unavailable"
    assert executor.calls == 1
    assert len(restarted.engine.store.all("eval_decisions")) == 1
    stored = restarted.engine.store.get("eval_waiters", "waiter_id", waiter_id)
    assert stored["text"] == ""


def test_evaluator_resume_from_different_cwd_uses_recorded_project(
    tmp_path, monkeypatch
):
    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    first = _router(tmp_path, monkeypatch, governor, executor)
    first.say("in proj_a: unrelated work")
    waiter_id = first.say("analyze the latest result").waiter_id
    first.engine.store.close()

    resume_dir = tmp_path / "resume-cwd"
    resume_dir.mkdir()
    monkeypatch.chdir(resume_dir)
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(resume_dir))
    restarted = _router(
        tmp_path,
        monkeypatch,
        governor,
        executor,
        project_dir=None,
    )

    resumed = restarted.resume_evaluation(waiter_id, manual=True)

    assert resumed.kind == "handoff"
    assert resumed.project == "proj_a"
    assert restarted.evaluator.project_dir == tmp_path.resolve()
    assert executor.calls == 1


def test_evaluator_resume_after_unrelated_handoff(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    router = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = router.say("analyze the latest result").waiter_id

    assert router.say("in proj_b: unrelated work").kind == "handoff"
    resumed = router.resume_evaluation(waiter_id, manual=True)

    assert resumed.kind == "handoff"
    assert resumed.project == "proj_a"
    assert executor.calls == 1
    assert len(router.engine.store.all("eval_decisions")) == 1


def test_evaluator_resume_refuses_route_change_without_decision(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    router = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = router.say("analyze the latest result").waiter_id
    router.evaluator.model = "codex/gpt-5.6-luna"
    router.evaluator.role_capabilities = {
        "claude-code": {"evaluator": {"accepted": True}},
        "codex": {"evaluator": {"accepted": True}},
    }

    refused = router.resume_evaluation(waiter_id, manual=True)

    assert refused.kind == "unavailable"
    assert "route changed" in refused.detail
    assert executor.calls == 0
    assert router.engine.store.all("eval_decisions") == []


def test_evaluator_resume_refuses_registry_change_without_decision(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    router = _router(tmp_path, monkeypatch, governor, executor)
    router.say("in proj_a: unrelated work")
    waiter_id = router.say("analyze the latest result").waiter_id
    router.registry["proj_a"]["inbox"] = str(tmp_path / "moved" / "inbox")

    refused = router.resume_evaluation(waiter_id, manual=True)

    assert refused.kind == "unavailable"
    assert "registry changed" in refused.detail
    assert executor.calls == 0
    assert router.engine.store.all("eval_decisions") == []


def test_evaluator_second_quota_stays_waiting_without_decision(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = QuotaExecutor()
    router = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = router.say("analyze the latest result").waiter_id
    governor.clock = lambda: datetime(2026, 11, 16, 0, 0, 2, tzinfo=timezone.utc)

    resumed = router.resume_evaluation(waiter_id, manual=True)

    assert resumed.kind == "waiting_quota"
    assert resumed.waiter_id == waiter_id
    assert executor.calls == 2
    assert router.engine.store.all("eval_decisions") == []
    stored = router.engine.store.get("eval_waiters", "waiter_id", waiter_id)
    assert stored["state"] == "pending"
    assert stored["text"] == "analyze the latest result"


def test_evaluator_resume_exception_never_leaves_running(tmp_path, monkeypatch):
    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    reset_at = datetime(2026, 11, 17, tzinfo=timezone.utc)
    route = resolve_route("evaluator", {"model": "claude-code/claude-haiku-5-5"})
    waiting = governor.checkpoint_limit(
        route.subscription_key,
        LimitInfo("weekly limit", reset_at, "fixture"),
    )
    router = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = router.say("analyze the latest result").waiter_id
    before = router.engine.store.get("eval_waiters", "waiter_id", waiter_id)
    monkeypatch.setattr(
        router.evaluator,
        "evaluate",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )

    reply = router.resume_evaluation(waiter_id, manual=True)

    assert reply.kind == "unavailable"
    stored = router.engine.store.get("eval_waiters", "waiter_id", waiter_id)
    assert stored["state"] == "pending"
    assert stored["hold_id"] == before["hold_id"] == waiting.hold_id
    assert stored["reset_at"] == before["reset_at"] == reset_at.isoformat()
    assert router.engine.store.all("eval_decisions") == []


def test_governed_opencode_evaluator_is_receipted_once(tmp_path, monkeypatch):
    from router_runner import evaluator as evaluator_module

    calls = []

    def fake_run_process(argv, cwd, env, timeout, stdout, stderr, control):
        calls.append((argv, cwd, env, timeout))
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text(
            '{"action":"accept","project":"proj_a","confidence":0.95,'
            '"reason":"clear"}',
            encoding="utf-8",
        )
        stderr.write_text("", encoding="utf-8")
        control.on_start(os.getpid(), os.getpgid(0))
        return ProcessOutcome(0, False, 0.01, stdout, stderr, outcome=RunOutcome("success"))

    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    monkeypatch.setattr(evaluator_module, "run_process", fake_run_process)
    governor = _governor(tmp_path)
    evaluator = GenerativeEvaluator(
        "opencode-go/deepseek-v4.1-flash",
        governor=governor,
        project_dir=tmp_path,
    )
    engine = agentd.get_engine(str(tmp_path / "opencode-router.db"), out_root=tmp_path / "runs")
    router = Router(
        engine,
        registry={"proj_a": {"inbox": str(tmp_path / "proj_a" / "inbox")}},
        evaluator=evaluator,
    )

    reply = router.say("analyze the latest result")

    assert reply.kind == "handoff"
    assert len(calls) == 1
    rows = [json.loads(line) for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["run_kind"] == "evaluator"
    assert "analyze the latest result" not in json.dumps(rows)
