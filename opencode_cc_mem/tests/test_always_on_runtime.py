"""Always-on runtime tests; no LLM, OpenCode process, or network required."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner.admission import AdmissionPolicy
from router_runner.agentd import operator
from router_runner.always_on import AlwaysOnRuntime
from router_runner.campaign_adapter import CompositeAdapter
from router_runner.engine import ControlEngine
from router_runner.job_service import FakeJobService
from router_runner.models import RouterError, WorkItemState
from router_runner.store import ControlStore


def make_runtime(tmp_path, *, auto=True, delivery=True):
    project = tmp_path / "mroot" / "opencode_cc_mem" / "projects" / "demo"
    (project / ".magnolia").mkdir(parents=True)
    (project / ".magnolia" / "boot-context.md").write_text("Project baseline.")
    store = ControlStore(str(tmp_path / "always-on.db"))
    engine = ControlEngine(
        store,
        AdmissionPolicy(
            allowed_senders=frozenset({operator()}),
            allowed_projects=frozenset({"demo"}),
        ),
        CompositeAdapter(out_root=str(project / "runs")),
        FakeJobService(project / "runs" / "jobs"),
    )
    runtime = AlwaysOnRuntime(
        engine,
        project,
        auto_execute=auto,
        delivery_dir="inbox/delivered" if delivery else None,
        memory_limit=12000,
    )
    return runtime, project


def event(event_id, text="do the work", thread="default", **kwargs):
    result = {
        "source_id": "test:inbox",
        "source_event_id": event_id,
        "thread_key": thread,
        "text": text,
    }
    result.update(kwargs)
    return result


def active_run(runtime):
    runs = runtime.store.all("ao_runs", "status IN ('bridging', 'running')")
    assert len(runs) == 1
    return runs[0]


def finish_legacy(run, body="FINAL ANSWER: complete", status="done"):
    letter = Path(run["letter_path"])
    letter.write_text(letter.read_text().replace("status: open", f"status: {status}", 1))
    Path(run["reply_path"]).write_text(body + "\n")


def test_manual_approval_legacy_bridge_memory_and_outbox(tmp_path):
    runtime, project = make_runtime(tmp_path, auto=False)
    result = runtime.ingest(event("one", "first request"))
    wi = result["work_item_id"]
    assert runtime.store.get("work_items", "work_item_id", wi)["state"] == "candidate"
    assert runtime.pump() is None
    assert list((project / "inbox").rglob("*.task.md")) == []

    runtime.approve(wi, result["approval"]["nonce"])
    run = runtime.pump()
    assert run["status"] == "running"
    letter = Path(run["letter_path"])
    text = letter.read_text()
    assert "first request" in text
    assert "Project baseline." in text
    assert "session-key:" in text
    assert runtime.pump() is None  # one active worker per lane

    finish_legacy(run)
    assert runtime.reconcile() == [{"run_id": run["run_id"], "outcome": "done"}]
    assert runtime.reconcile() == []  # terminal runs are not processed twice
    completed_wi = runtime.store.get("work_items", "work_item_id", wi)
    assert completed_wi["state"] == "completed"
    assert completed_wi["outcome"] == "completed"
    assert runtime.store.get("ao_events", "event_id", result["event"]["event_id"])["status"] == "done"
    progress = runtime.memory.progress_path("default")
    assert "FINAL ANSWER: complete" in progress.read_text()

    outbox = runtime.store.all("ao_outbox", "run_id = ?", (run["run_id"],))
    assert len(outbox) == 1 and outbox[0]["state"] == "pending"
    first = runtime.dispatch()
    second = runtime.dispatch()
    assert len(first) == 1 and second == []
    assert Path(first[0]["destination"]).exists()
    assert runtime.store.get("ao_outbox", "message_id", outbox[0]["message_id"])["state"] == "sent"

    duplicate = runtime.ingest(event("one", "first request"))
    assert duplicate["created"] is False
    assert len(runtime.store.all("ao_events", "1=1")) == 1


def test_auto_approval_batches_one_thread_and_serializes_threads(tmp_path):
    runtime, project = make_runtime(tmp_path, auto=True)
    one = runtime.ingest(event("a1", "part one", thread="a"))
    two = runtime.ingest(event("a2", "part two", thread="a"))
    runtime.ingest(event("b1", "other thread", thread="b"))
    assert one["work_item_id"] == two["work_item_id"]

    run = runtime.pump()
    letter = Path(run["letter_path"]).read_text()
    assert "part one" in letter and "part two" in letter
    assert "other thread" not in letter
    assert len(runtime.store.all("ao_runs", "1=1")) == 1
    assert runtime.pump() is None

    finish_legacy(run)
    runtime.reconcile()
    second = runtime.pump()
    assert second["run_id"] != run["run_id"]
    assert second["session_key"] != run["session_key"]
    assert "other thread" in Path(second["letter_path"]).read_text()


def test_followup_and_schedule_timers_emit_durable_events(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    followup = runtime.add_timer({
        "kind": "followup", "due_at": now.isoformat(),
        "thread_key": "timer-thread", "text": "check for a reply",
    })
    schedule = runtime.add_timer({
        "kind": "schedule", "due_at": now.isoformat(),
        "recurrence_seconds": 3600, "thread_key": "timer-thread",
        "text": "run the hourly sweep",
    })
    fired = runtime.fire_due_timers(now)
    assert len(fired) == 2
    assert runtime.store.get("ao_timers", "timer_id", followup["timer_id"])["status"] == "done"
    advanced = runtime.store.get("ao_timers", "timer_id", schedule["timer_id"])
    assert advanced["status"] == "active"
    assert advanced["due_at"] == (now + timedelta(hours=1)).isoformat()
    assert runtime.fire_due_timers(now) == []
    assert len(runtime.store.all("ao_events", "1=1")) == 2


def test_waiting_human_answer_resumes_same_work_item_and_session(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    first = runtime.ingest(event("question", "investigate"))
    wi = first["work_item_id"]
    run1 = runtime.pump()
    finish_legacy(run1, "FINAL ANSWER: Which budget should I use?\nalways-on-status: waiting-human")
    runtime.reconcile()
    assert runtime.store.get("work_items", "work_item_id", wi)["state"] == WorkItemState.WAITING_HUMAN
    assert runtime.store.all("ao_outbox", "kind = ?", ("question",))[0]["state"] == "pending"

    answer = runtime.ingest(event("answer", "use the existing budget"))
    assert answer["work_item_id"] == wi
    assert answer["approval"] is None
    assert runtime.store.get("work_items", "work_item_id", wi)["state"] == WorkItemState.QUEUED
    run2 = runtime.pump()
    assert run2["work_item_id"] == wi
    assert run2["session_key"] == run1["session_key"]
    assert "use the existing budget" in Path(run2["letter_path"]).read_text()


def test_event_arriving_mid_run_gets_linked_fresh_work_item(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    first = runtime.ingest(event("first", "batch one"))
    run1 = runtime.pump()
    second = runtime.ingest(event("second", "batch two"))
    assert second["work_item_id"] == first["work_item_id"]
    finish_legacy(run1)
    runtime.reconcile()

    run2 = runtime.pump()
    assert run2["run_id"] != run1["run_id"]
    assert run2["work_item_id"] != run1["work_item_id"]
    wi2 = runtime.store.get("work_items", "work_item_id", run2["work_item_id"])
    assert wi2["parent_work_item_id"] == run1["work_item_id"]
    assert run2["session_key"] == run1["session_key"]
    assert "batch two" in Path(run2["letter_path"]).read_text()


def test_bridging_crash_reemits_once_but_running_letter_is_not_duplicated(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    runtime.ingest(event("crash", "work"))
    run = runtime.pump()
    letter = Path(run["letter_path"])
    original = letter.read_text()
    with runtime.store.tx():
        runtime.store.update("ao_runs", "run_id", run["run_id"], {"status": "bridging"})
    runtime.reconcile()
    assert letter.read_text() == original
    assert len(list(letter.parent.glob("*.task.md"))) == 1

    # Once running, a missing letter fails closed rather than risking a second
    # agent execution after an unknown external outcome.
    letter.unlink()
    runtime.reconcile()
    assert runtime.store.get("ao_runs", "run_id", run["run_id"])["status"] == "failed"
    failed_wi = runtime.store.get("work_items", "work_item_id", run["work_item_id"])
    assert failed_wi["state"] == "failed" and failed_wi["outcome"] == "failed"
    assert not letter.exists()
    assert runtime.store.all("ao_outbox", "kind = ?", ("error",))


def test_no_send_is_durable_and_never_dispatched(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    runtime.ingest(event("quiet", "check silently"))
    run = runtime.pump()
    finish_legacy(run, "FINAL ANSWER: nothing needs attention\nalways-on-no-send: yes")
    runtime.reconcile()
    row = runtime.store.all("ao_outbox", "run_id = ?", (run["run_id"],))[0]
    assert row["state"] == "suppressed"
    assert runtime.dispatch() == []


def test_scope_change_cannot_ride_an_active_approval(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    spec = {"project": "demo", "protocol": "legacy-mailbox-v0.1",
            "scope": "s1", "budget": "b1"}
    runtime.ingest(event("scoped", "first", work_spec=spec))
    with pytest.raises(RouterError, match="WorkSpec cannot change"):
        runtime.ingest(event("changed", "second", work_spec={**spec, "budget": "b2"}))
    changed = runtime.store.find_one("ao_events", "source_event_id = ?", ("changed",))
    assert changed["status"] == "error"


def test_runtime_requires_explicit_project_opt_in(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    with pytest.raises(RouterError, match="not registered"):
        AlwaysOnRuntime.from_project(str(tmp_path / "x.db"), project)
    (project / "agent.json").write_text(json.dumps({"enabled": True}))
    with pytest.raises(RouterError, match="opt-in"):
        AlwaysOnRuntime.from_project(str(tmp_path / "x.db"), project)
    (project / "agent.json").write_text(json.dumps({
        "enabled": True,
        "always_on": {"enabled": True, "auto_execute": False},
    }))
    runtime = AlwaysOnRuntime.from_project(str(tmp_path / "x.db"), project)
    try:
        assert runtime.auto_execute is False
        assert runtime.delivery_dir is None
    finally:
        runtime.close()


def test_real_legacy_handle_task_boundary(tmp_path):
    """The runtime letter works with the unmodified legacy handle_task flow."""
    import importlib.util
    from importlib.machinery import SourceFileLoader

    script = (Path(__file__).resolve().parents[1] / "softwares" / "bin"
              / "magnolia-agents-daemon")
    spec = importlib.util.spec_from_file_location(
        "agents_daemon_for_always_on", script,
        loader=SourceFileLoader("agents_daemon_for_always_on", str(script)),
    )
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)

    runtime, project = make_runtime(tmp_path)
    runtime.ingest(event("legacy-boundary", "use the real mailbox"))
    run = runtime.pump()
    seen = {}

    def runner(agent, project_dir, root, prompt, timeout_s, **kwargs):
        seen.update(kwargs)
        assert project_dir == str(project)
        assert "use the real mailbox" in prompt
        return "FINAL ANSWER: legacy executor complete"

    root = str(project.parents[2])
    assert daemon.handle_task(
        Path(run["letter_path"]), "demo", root, runner=runner
    ) == "done"
    assert seen == {"session_key": run["session_key"]}
    assert runtime.reconcile()[0]["outcome"] == "done"
    assert runtime.store.get("ao_runs", "run_id", run["run_id"])["status"] == "completed"


def test_terminal_scope_change_gets_fresh_work_item_and_approval(tmp_path):
    runtime, _ = make_runtime(tmp_path, auto=False)
    spec1 = {"project": "demo", "protocol": "legacy-mailbox-v0.1",
             "scope": "s1", "budget": "b1"}
    first = runtime.ingest(event("scope-one", "first", work_spec=spec1))
    runtime.approve(first["work_item_id"], first["approval"]["nonce"])
    run = runtime.pump()
    finish_legacy(run)
    runtime.reconcile()

    spec2 = {**spec1, "scope": "s2", "budget": "b2"}
    second = runtime.ingest(event("scope-two", "second", work_spec=spec2))
    assert second["work_item_id"] != first["work_item_id"]
    assert second["approval"]["nonce"]
    wi2 = runtime.store.get("work_items", "work_item_id", second["work_item_id"])
    assert wi2["state"] == WorkItemState.CANDIDATE
    assert wi2["scope"] == "s2" and wi2["budget"] == "b2"
    assert wi2["parent_work_item_id"] == first["work_item_id"]


def test_mailbox_protocol_cannot_execute_as_fake_campaign(tmp_path):
    runtime, _ = make_runtime(tmp_path, auto=False)
    result = runtime.ingest(event("no-fake", "mailbox only"))
    runtime.approve(result["work_item_id"], result["approval"]["nonce"])
    with pytest.raises(RouterError, match="not the campaign adapter"):
        runtime.engine.execute(result["work_item_id"])
    wi = runtime.store.get("work_items", "work_item_id", result["work_item_id"])
    assert wi["state"] == WorkItemState.QUEUED
    assert runtime.store.all("assignments", "work_item_id = ?", (wi["work_item_id"],)) == []
    assert runtime.store.all("effects", "work_item_id = ?", (wi["work_item_id"],)) == []


@pytest.mark.parametrize("auto", [False, True])
def test_approved_queued_work_recovers_waiting_approval_events(tmp_path, auto):
    """A used nonce must not strand events after a state-only interruption."""
    runtime, _ = make_runtime(tmp_path, auto=auto)
    result = runtime.ingest(event("approval-recovery", "work"))
    wi = result["work_item_id"]
    if not auto:
        runtime.approve(wi, result["approval"]["nonce"])
    with runtime.store.tx():
        runtime.store.update("ao_events", "event_id", result["event"]["event_id"], {
            "status": "waiting_approval",
        })
    assert runtime.store.get("work_items", "work_item_id", wi)["state"] == "queued"
    run = runtime.pump()
    assert run is not None and run["work_item_id"] == wi
    assert runtime.store.get("ao_events", "event_id", result["event"]["event_id"])["status"] == "leased"


def test_existing_trigger_and_candidate_resume_without_duplicate(tmp_path):
    """Simulate the pre-atomic crash between request_work and event linkage."""
    runtime, _ = make_runtime(tmp_path, auto=True)
    result = runtime.ingest(event("partial-create", "work"))
    wi = result["work_item_id"]
    with runtime.store.tx():
        runtime.store.update("ao_events", "event_id", result["event"]["event_id"], {
            "work_item_id": None, "status": "pending",
        })
    run = runtime.pump()
    assert run is not None and run["work_item_id"] == wi
    assert len(runtime.store.all("work_items", "1=1")) == 1
    assert len(runtime.store.all("triggers", "1=1")) == 1


def test_mid_run_input_resumes_waiting_human_without_another_event(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    first = runtime.ingest(event("wait-first", "investigate"))
    run1 = runtime.pump()
    second = runtime.ingest(event("wait-second", "the answer is 42"))
    assert second["work_item_id"] == first["work_item_id"]
    finish_legacy(run1, "FINAL ANSWER: Need your answer\nalways-on-status: waiting-human")
    runtime.reconcile()
    assert runtime.store.get("work_items", "work_item_id", first["work_item_id"])["state"] == "waiting_human"

    run2 = runtime.pump()
    assert run2 is not None
    assert run2["work_item_id"] == first["work_item_id"]
    assert run2["session_key"] == run1["session_key"]
    assert "the answer is 42" in Path(run2["letter_path"]).read_text()


def test_timer_failure_is_visible_due_and_does_not_kill_other_work(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    spec1 = {"project": "demo", "protocol": "legacy-mailbox-v0.1",
             "scope": "s1", "budget": "b1"}
    runtime.ingest(event("timer-scope", "active work", thread="shared", work_spec=spec1))
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    timer = runtime.add_timer({
        "kind": "followup", "due_at": now.isoformat(), "thread_key": "shared",
        "text": "expanded work", "work_spec": {**spec1, "scope": "s2"},
    })
    first = runtime.fire_due_timers(now)
    assert len(first) == 1 and "WorkSpec cannot change" in first[0]["error"]
    current = runtime.store.get("ao_timers", "timer_id", timer["timer_id"])
    assert current["status"] == "active" and current["due_at"] == timer["due_at"]
    assert current["error_count"] == 1

    second = runtime.fire_due_timers(now)
    assert len(second) == 1 and second[0]["error"]
    current = runtime.store.get("ao_timers", "timer_id", timer["timer_id"])
    assert current["due_at"] == timer["due_at"] and current["error_count"] == 2
    # The ordinary event is still executable; a bad timer did not kill the pump.
    assert runtime.pump() is not None


def test_non_mailbox_protocol_is_rejected_before_event_creation(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    spec = {"project": "demo", "protocol": "demo-docking-v1",
            "scope": "s", "budget": "b"}
    with pytest.raises(RouterError, match="require protocol=legacy-mailbox"):
        runtime.ingest(event("wrong-protocol", "work", work_spec=spec))
    assert runtime.store.all("ao_events", "1=1") == []


def test_human_stop_requires_manual_approval_even_on_auto_lane(tmp_path):
    runtime, _ = make_runtime(tmp_path, auto=True)
    first = runtime.ingest(event("stop-first", "work"))
    run = runtime.pump()
    runtime.engine.stop(first["work_item_id"], "operator:test")
    finish_legacy(run)
    runtime.reconcile()

    second = runtime.ingest(event("stop-second", "new request"))
    wi2 = runtime.store.get("work_items", "work_item_id", second["work_item_id"])
    assert wi2["state"] == WorkItemState.CANDIDATE
    assert second["event"]["status"] == "waiting_approval"
    assert runtime.pump() is None


def clone_runtime(runtime):
    engine = ControlEngine(
        ControlStore(runtime.store.db_path),
        runtime.engine.policy,
        CompositeAdapter(out_root=str(runtime.project_dir / "runs")),
        FakeJobService(runtime.project_dir / "runs" / "jobs"),
    )
    return AlwaysOnRuntime(
        engine,
        runtime.project_dir,
        auto_execute=runtime.auto_execute,
        delivery_dir=runtime.delivery_dir,
    )


def test_two_connections_share_one_thread_work_item(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    first, _ = make_runtime(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(lambda: clone_runtime(first).ingest(
                event("concurrent-a", "A", thread="shared"))),
            pool.submit(lambda: clone_runtime(first).ingest(
                event("concurrent-b", "B", thread="shared"))),
        ]
        results = [f.result() for f in futures]
    assert results[0]["work_item_id"] == results[1]["work_item_id"]
    assert len(first.store.all("work_items", "1=1")) == 1


def test_two_connections_reconcile_and_dispatch_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    first, _ = make_runtime(tmp_path)
    first.ingest(event("concurrent-finish", "work"))
    run = first.pump()
    finish_legacy(run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lambda: clone_runtime(first).reconcile()) for _ in range(2)]
        results = [f.result() for f in futures]
    assert sum(len(rows) for rows in results) == 1
    assert len(first.store.all("ao_outbox", "1=1")) == 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lambda: clone_runtime(first).dispatch()) for _ in range(2)]
        delivered = [f.result() for f in futures]
    assert sum(len(rows) for rows in delivered) == 1
    row = first.store.all("ao_outbox", "1=1")[0]
    assert row["state"] == "sent"
    assert len(list(Path(row["destination"]).parent.glob("*.md"))) == 1


def test_concurrent_bridging_emission_never_reopens_terminal_letter(tmp_path):
    """A late publisher must not overwrite done/error with status=open."""
    first, _ = make_runtime(tmp_path)
    first.ingest(event("publish-race", "work"))
    run = first.pump()
    second = clone_runtime(first)
    with first.store.tx():
        first.store.update("ao_runs", "run_id", run["run_id"], {"status": "bridging"})
    letter = Path(run["letter_path"])
    letter.unlink()

    # Both recovery paths hold the same stale bridging row. The first publishes;
    # the daemon finishes before the second publisher gets its turn.
    first._emit_run(run)
    finish_legacy(run, "FINAL ANSWER: finished before second publish")
    second._emit_run(run)
    assert "status: done" in letter.read_text().splitlines()[0]
    assert "status: open" not in letter.read_text().splitlines()[0]

    processed = second.reconcile()
    assert processed == [{"run_id": run["run_id"], "outcome": "done"}]
    current = first.store.get("ao_runs", "run_id", run["run_id"])
    assert current["status"] == "completed"
    assert len(first.store.all("ao_outbox", "run_id = ?", (run["run_id"],))) == 1
    # A late CAS from the stale publisher cannot resurrect the finalized run.
    assert not second._mark_running(run["run_id"])
    assert first.store.get("ao_runs", "run_id", run["run_id"])["status"] == "completed"
