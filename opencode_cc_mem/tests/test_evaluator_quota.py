from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.governor import Governor, Policy
from campaign_runner.process import ProcessOutcome
from campaign_runner.routing import resolve_route
from router_runner import agentd
from router_runner.evaluator import EVALUATOR_SYSTEM_PROMPT, EvalDecision, GenerativeEvaluator
from router_runner.router import Router
from router_runner.store import ControlStore


CAPABILITIES = {"claude-code": {"evaluator": {"accepted": True}}}


class DecisionExecutor:
    def __init__(self):
        self.calls = 0

    def run(self, prompt, **kwargs):
        self.calls += 1
        control = kwargs.get("control")
        if control is not None and control.on_start is not None:
            control.on_start(os.getpid(), os.getpgid(0))
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
        control = kwargs.get("control")
        if control is not None and control.on_start is not None:
            control.on_start(os.getpid(), os.getpgid(0))
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


def test_setup_selected_client_controls_dispatch(tmp_path, monkeypatch):
    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr(
        evaluator_module,
        "load_setup",
        lambda _root: {"routes": {"evaluator": "claude-code/claude-haiku-5-5"}},
    )
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    monkeypatch.setattr(evaluator_module.llm, "is_llm_available", lambda: False)
    evaluator = GenerativeEvaluator(
        governor=_governor(tmp_path),
        project_dir=tmp_path,
        role_capabilities=CAPABILITIES,
    )

    assert evaluator.route_identity()["client"] == "claude-code"
    assert evaluator._uses_client() is True
    assert evaluator.available() is True


def test_explicit_openrouter_evaluator_keeps_direct_api_path(tmp_path):
    evaluator = GenerativeEvaluator(
        "openrouter/deepseek/deepseek-v4.1-flash",
        governor=_governor(tmp_path),
        project_dir=tmp_path,
    )

    assert evaluator.route_identity()["client"] == "api"
    assert evaluator._uses_client() is False


def test_opencode_go_evaluator_keeps_base_prompt_and_two_steps(
    tmp_path, monkeypatch
):
    from router_runner import evaluator as evaluator_module

    prompts = []
    route = resolve_route(
        "evaluator", {"model": "opencode-go/deepseek-v4.1-flash"}
    )
    evaluator = GenerativeEvaluator(
        "opencode-go/deepseek-v4.1-flash",
        governor=_governor(tmp_path),
        project_dir=tmp_path,
    )
    monkeypatch.setattr(evaluator_module, "executor_for", lambda *args: object())
    monkeypatch.setattr(evaluator, "_admit", lambda *args: None)

    def run_executor(_executor, exec_context, prompt, _waiter, _admission):
        prompts.append((exec_context, prompt))
        return SimpleNamespace(
            final_text=(
                '{"action":"ignore","project":null,"confidence":1,'
                '"reason":"ok"}'
            ),
            outcome=RunOutcome("success"),
            usage={},
            attempts=1,
            duration_seconds=0.01,
        )

    monkeypatch.setattr(evaluator, "_run_executor", run_executor)

    result = evaluator._call_client("hello", {}, None, route=route)

    assert result.outcome.kind == "success"
    exec_context, prompt = prompts[0]
    assert exec_context.role_cap == 2
    assert prompt == (
        EVALUATOR_SYSTEM_PROMPT + '\n\n{"message": "hello", "context": {}}'
    )


def test_evaluator_context_satisfies_native_launch_contract(tmp_path, monkeypatch):
    from campaign_runner.launch import _require_context
    from router_runner import evaluator as evaluator_module

    monkeypatch.setenv("MAGNOLIA_TOOLS_PORT", "9123")
    monkeypatch.setattr(evaluator_module, "load_setup", lambda _root: {})
    route = resolve_route("evaluator", {"model": "claude-code/claude-haiku-5-5"})
    evaluator = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        governor=_governor(tmp_path),
        project_dir=tmp_path,
        role_capabilities=CAPABILITIES,
    )

    context = evaluator._execution_context(route)

    _require_context(context)
    assert context.control_dir == context.run_dir / "control"
    assert context.work_dir == context.run_dir / "work"
    assert context.tools_port == 9123
    assert context.environment["MAGNOLIA_DOORBELL"] == "0"


def test_native_evaluator_usage_keeps_cache_read_tokens():
    result = SimpleNamespace(
        usage={
            "input_tokens": 10,
            "output_tokens": 2,
            "reasoning_tokens": 1,
            "cache_read_tokens": 7,
            "cache_write_tokens": 3,
        },
        attempts=1,
        duration_seconds=0.1,
        session_identity=None,
    )

    usage = GenerativeEvaluator._usage(result)

    assert usage.cached_tokens == 7
    assert usage.completeness == "complete"


def test_evaluator_closes_executor_and_removes_per_call_directory(
    tmp_path, monkeypatch,
):
    route = resolve_route("evaluator", {"model": "claude-code/claude-haiku-5-5"})
    evaluator = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        project_dir=tmp_path,
        role_capabilities=CAPABILITIES,
    )
    closed = []

    class Executor:
        def close(self):
            closed.append(True)

    def make_executor(_route, _role, context):
        context.run_dir.mkdir(parents=True)
        (context.run_dir / "materialized-config").write_text("temporary")
        return Executor()

    monkeypatch.setattr("router_runner.evaluator.executor_for", make_executor)
    monkeypatch.setattr(
        evaluator,
        "_run_executor",
        lambda *args: SimpleNamespace(
            final_text='{"action":"ignore","project":null,"confidence":1,"reason":"ok"}',
            outcome=RunOutcome("success"),
            usage={},
            attempts=1,
            duration_seconds=0.01,
            session_identity=None,
        ),
    )

    result = evaluator._call_client("hello", {}, None, route=route)

    assert result.outcome.kind == "success"
    assert closed == [True]
    runs = tmp_path / ".magnolia" / "evaluator-runs"
    assert not runs.exists() or list(runs.iterdir()) == []


def test_failed_evaluator_output_is_never_parsed_as_accept(tmp_path, monkeypatch):
    evaluator = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        governor=_governor(tmp_path),
        project_dir=tmp_path,
        role_capabilities=CAPABILITIES,
    )
    monkeypatch.setattr(evaluator, "available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        evaluator,
        "_call_client",
        lambda *args, **kwargs: SimpleNamespace(
            text='{"action":"accept","project":"proj_a","confidence":1,"reason":"bad"}',
            outcome=RunOutcome("failure"),
        ),
    )

    assert evaluator.evaluate("route this", {"projects": ["proj_a"]}) is None


def test_typed_failure_with_terminal_rate_limit_becomes_quota():
    result = SimpleNamespace(
        outcome=RunOutcome("failure"),
        returncode=1,
        final_text="",
        stdout="",
        stderr="RateLimitError: weekly limit reached",
        errors=[],
    )

    outcome = GenerativeEvaluator._client_outcome(result)

    assert outcome.kind == "quota"
    assert outcome.limit.evidence_source == "evaluator client diagnostics"


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


def test_crashed_evaluator_claim_is_recoverable(tmp_path, monkeypatch):
    from campaign_runner import governor as governor_module

    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    router = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = router.say("analyze the latest result").waiter_id
    store = router.engine.store
    store.update(
        "eval_waiters",
        "waiter_id",
        waiter_id,
        {
            "state": "running",
            "owner_pid": 99999999,
            "owner_start_token": "proc:dead",
        },
    )
    store.conn.commit()
    monkeypatch.setattr(governor_module, "_pid_gone", lambda pid: pid == 99999999)

    resumed = router.resume_evaluation(waiter_id, manual=True)

    assert resumed.kind == "handoff"
    assert executor.calls == 1
    assert store.get("eval_waiters", "waiter_id", waiter_id)["state"] == "completed"


def test_two_concurrent_evaluator_resumers_claim_exactly_once(tmp_path, monkeypatch):
    database = tmp_path / "claim-race.db"
    seed = ControlStore(database)
    with seed.tx():
        seed.insert(
            "eval_waiters",
            {
                "waiter_id": "waiter-race",
                "text": "evaluate",
                "context": "[]",
                "subscription_key": "claude-code",
                "hold_id": None,
                "reset_at": None,
                "route_model": "claude-code/model",
                "state": "pending",
                "owner_pid": None,
                "owner_start_token": None,
                "decision_json": None,
                "reply_json": None,
                "created_at": "2026-11-17T00:00:00+00:00",
                "updated_at": "2026-11-17T00:00:00+00:00",
            },
        )
    seed.close()
    both_read_pending = threading.Barrier(2)
    original_get = ControlStore.get

    def synchronized_get(store, table, key_col, key):
        row = original_get(store, table, key_col, key)
        if table == "eval_waiters" and key == "waiter-race":
            both_read_pending.wait(timeout=5)
        return row

    monkeypatch.setattr(ControlStore, "get", synchronized_get)
    claims: list[bool] = []
    errors: list[BaseException] = []

    def claim():
        store = ControlStore(database)
        try:
            claims.append(
                store.claim_eval_waiter("waiter-race", "2026-11-17T00:00:01+00:00")
                is not None
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert claims.count(True) == 1
    assert claims.count(False) == 1


def test_crashed_evaluator_replays_checkpointed_decision_without_second_call(
    tmp_path, monkeypatch
):
    from campaign_runner import governor as governor_module

    governor = _governor(tmp_path)
    executor = DecisionExecutor()
    _unknown_hold(governor)
    router = _router(tmp_path, monkeypatch, governor, executor)
    waiter_id = router.say("analyze the latest result").waiter_id
    decision = EvalDecision("accept", "proj_a", 0.95, "clear", "generative")
    decision_json = json.dumps(decision.__dict__, sort_keys=True)
    router.engine.record_waiter_eval_decision(
        waiter_id,
        "analyze the latest result",
        decision,
        decision_json,
        datetime.now(timezone.utc).isoformat(),
    )
    store = router.engine.store
    store.update(
        "eval_waiters",
        "waiter_id",
        waiter_id,
        {
            "state": "running",
            "owner_pid": 99999999,
            "owner_start_token": "proc:dead",
        },
    )
    store.conn.commit()
    monkeypatch.setattr(governor_module, "_pid_gone", lambda pid: pid == 99999999)
    monkeypatch.setattr(router.evaluator, "available", lambda: False)

    resumed = router.resume_evaluation(waiter_id, manual=True)

    assert resumed.kind == "handoff"
    assert executor.calls == 0
    assert len(list((tmp_path / "proj_a" / "inbox").glob("route-eval-*.task.md"))) == 1
    stored = store.get("eval_waiters", "waiter_id", waiter_id)
    assert stored["state"] == "completed"
    assert json.loads(stored["reply_json"])["kind"] == "handoff"
    assert len(store.all("eval_decisions")) == 1


def test_evaluator_handoff_application_is_idempotent(tmp_path, monkeypatch):
    router = _router(tmp_path, monkeypatch, _governor(tmp_path), DecisionExecutor())
    decision = EvalDecision("accept", "proj_a", 0.95, "clear", "generative")

    first = router._apply_eval(
        decision, "analyze the latest result", [], idempotency_key="waiter-1"
    )
    second = router._apply_eval(
        decision, "analyze the latest result", [], idempotency_key="waiter-1"
    )

    assert first == second
    assert len(list((tmp_path / "proj_a" / "inbox").glob("route-eval-*.task.md"))) == 1


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
