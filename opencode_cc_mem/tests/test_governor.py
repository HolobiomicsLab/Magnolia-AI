import json
from datetime import date, datetime, timedelta, timezone

import pytest

from campaign_runner.executors.contracts import LimitInfo, RunOutcome, UsageRecord
from campaign_runner.governor import (
    Admission,
    AdmissionRequest,
    Governor,
    Policy,
    PolicyError,
    QuotaWaiting,
)
from campaign_runner.routing import ResolvedRoute


class FakeClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += timedelta(seconds=seconds)


def route(provider="anthropic", client="opencode"):
    key = client if client != "opencode" else f"opencode:{provider}"
    return ResolvedRoute(client, f"{provider}/model", provider, key, "worker", "test")


def request(identifier, selected=None, cap=80):
    return AdmissionRequest(
        route=selected or route(),
        invocation_id=identifier,
        parent_run_id="campaign-1",
        project_digest="project-digest",
        run_kind="worker",
        role_cap=cap,
    )


def governor(tmp_path, clock, **overrides):
    values = {
        "max_concurrent": 2,
        "max_runs_per_day": 50,
        "max_turns_per_run": 60,
        "min_seconds_between_runs": 30,
    }
    values.update(overrides)
    return Governor(
        Policy(**values),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
    )


def finish(admission, outcome="success", usage=None):
    admission.mark_started(pid=101, pgid=101, process_start_token="proc-1")
    admission.release(usage=usage, outcome=RunOutcome(outcome))


def test_slots_hold_across_processes(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    first = governor(tmp_path, clock, max_concurrent=1, min_seconds_between_runs=1)
    second = governor(tmp_path, clock, max_concurrent=1, min_seconds_between_runs=1)

    held = first.admit(request("one"))
    blocked = second.admit(request("two"))

    assert isinstance(held, Admission)
    assert isinstance(blocked, QuotaWaiting)
    assert blocked.reason == "concurrency"


def test_last_daily_admission_race(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    first = governor(tmp_path, clock, max_runs_per_day=1, min_seconds_between_runs=1)
    second = governor(tmp_path, clock, max_runs_per_day=1, min_seconds_between_runs=1)

    assert isinstance(first.admit(request("one")), Admission)
    blocked = second.admit(request("two", route("anthropic")))

    assert isinstance(blocked, QuotaWaiting)
    assert blocked.reason == "daily_limit"


def test_spacing_and_cancel_without_lock_held(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock)
    reserved = usage.admit(request("one"))
    assert isinstance(reserved, Admission)
    assert usage.admit(request("two")).reason == "spacing"

    reserved.cancel_before_launch()
    assert isinstance(usage.admit(request("three")), Admission)


def test_same_provider_shares_key_other_keys_independent(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, max_concurrent=1, min_seconds_between_runs=1)
    assert isinstance(usage.admit(request("a", route("anthropic"))), Admission)
    assert usage.admit(request("b", route("anthropic"))).reason == "concurrency"
    assert isinstance(usage.admit(request("c", route("openai"))), Admission)


def test_unknown_provider_throttled_api_provider_only_recorded(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    policy = Policy(api_key_providers=("openai",))
    usage = Governor(policy, ledger_path=tmp_path / "usage-ledger.jsonl", clock=clock)

    api = usage.admit(request("api", route("openai"), cap=17))
    unknown = usage.admit(request("unknown", route("mystery")))
    blocked = usage.admit(request("unknown-2", route("mystery")))

    assert isinstance(api, Admission) and api.effective_cap == 17
    assert isinstance(unknown, Admission) and unknown.effective_cap == 60
    assert isinstance(blocked, QuotaWaiting)
    finish(api)
    assert json.loads((tmp_path / "usage-ledger.jsonl").read_text())["billing"] == "api"


def test_xdg_and_missing_vs_invalid_policy(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    assert Policy.load() == Policy()
    with pytest.raises(PolicyError):
        Policy.load(tmp_path / "missing.yaml")
    policy_path = config_home / "magnolia" / "usage-policy.yaml"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("max_concurrent: 0\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        Policy.load()


@pytest.mark.parametrize("kind", ["failure", "timeout", "cancelled"])
def test_release_idempotent_on_exception_timeout_cancel(tmp_path, kind):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    admitted = usage.admit(request(kind))
    admitted.mark_started(pid=101, pgid=101, process_start_token="proc-1")
    admitted.release(outcome=RunOutcome(kind))
    admitted.release(outcome=RunOutcome("success"))
    rows = (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["outcome"]["kind"] == kind


def test_dead_owner_live_child_not_reclaimed(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, max_concurrent=1, min_seconds_between_runs=1)
    admitted = usage.admit(request("one"))
    admitted.mark_started(pid=202, pgid=202, process_start_token="birth")

    assert usage.reconcile("one", process_ended=False, service_request_ended=True) is False
    assert usage.admit(request("two")).reason == "concurrency"


def test_ended_work_reclaimed_once(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, max_concurrent=1, min_seconds_between_runs=1)
    usage.admit(request("one")).mark_started(
        pid=202, pgid=202, process_start_token="birth", service_request_id="remote-1"
    )
    assert usage.reconcile("one", process_ended=True, service_request_ended=True)
    assert not usage.reconcile("one", process_ended=True, service_request_ended=True)
    assert len((tmp_path / "usage-ledger.jsonl").read_text().splitlines()) == 1


def test_pending_completion_recovery_does_not_duplicate(tmp_path, monkeypatch):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    admitted = usage.admit(request("one"))
    admitted.mark_started(pid=202, pgid=202, process_start_token="birth")
    append_once = usage._append_once
    monkeypatch.setattr(
        usage,
        "_append_once",
        lambda _record: (_ for _ in ()).throw(RuntimeError("interrupted append")),
    )
    with pytest.raises(RuntimeError, match="interrupted append"):
        admitted.release(outcome=RunOutcome("failure"))
    monkeypatch.setattr(usage, "_append_once", append_once)

    usage.report(date(2026, 11, 15))
    usage.report(date(2026, 11, 15))
    assert len((tmp_path / "usage-ledger.jsonl").read_text().splitlines()) == 1


def test_cumulative_usage_delta_not_recounted(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    first_raw = {"tokens": {"input": 100, "output": 20}}
    first_usage = UsageRecord.from_raw(first_raw)
    first = usage.admit(request("one"))
    finish(first, usage=first_usage)
    clock.advance(1)
    delta = UsageRecord.from_raw(
        {"tokens": {"input": 150, "output": 30}}, baseline=first_usage
    )
    second = usage.admit(request("two"))
    finish(second, usage=delta)
    rows = [json.loads(line) for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()]
    assert rows[1]["usage"]["input_tokens"] == 50
    assert rows[1]["usage"]["output_tokens"] == 10


def test_shared_quota_blocks_second_process(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    first = governor(tmp_path, clock, min_seconds_between_runs=1)
    second = governor(tmp_path, clock, min_seconds_between_runs=1)
    admitted = first.admit(request("one"))
    admitted.mark_started(pid=1, pgid=1, process_start_token="birth")
    admitted.release(
        outcome=RunOutcome(
            "quota",
            LimitInfo("subscription quota", clock() + timedelta(hours=1), "stderr"),
        )
    )
    blocked = second.admit(request("two"))
    assert blocked.reason == "quota"
    clock.advance(3600)
    assert isinstance(second.admit(request("three")), Admission)


def test_waiters_visible_without_completed_runs(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, max_concurrent=1, min_seconds_between_runs=1)
    usage.admit(request("one"))
    usage.admit(request("two"))
    report = usage.report(date(2026, 11, 15))
    assert report["day"]["runs"] == 0
    assert report["waits"][0]["caller_id"] == "two"


def test_ledger_has_no_prompt_reply_or_key(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    admitted = usage.admit(request("one"))
    finish(admitted)
    body = (tmp_path / "usage-ledger.jsonl").read_text()
    assert "prompt" not in body
    assert "reply" not in body
    assert "api_key" not in body
    assert "secret" not in body


def test_unknown_quota_requires_explicit_resume(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    admitted = usage.admit(request("one"))
    finish(admitted, outcome="quota")
    clock.advance(86400)
    assert usage.admit(request("two")).reason == "quota"
    assert usage.resume_key("opencode:anthropic")
    assert isinstance(usage.admit(request("three")), Admission)
