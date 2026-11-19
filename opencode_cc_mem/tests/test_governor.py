import errno
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from campaign_runner.executors.contracts import LimitInfo, RunOutcome, UsageRecord
from campaign_runner import governor as governor_module
from campaign_runner.governor import (
    Admission,
    AdmissionRequest,
    Governor,
    Policy,
    PolicyError,
    QuotaWaiting,
)
from campaign_runner.process import ProcessStartError
from campaign_runner.routing import ResolvedRoute
from compchem_memory import process_identity


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


def _race_admit(ledger, policy_values, when, barrier, results, identifier):
    usage = Governor(
        Policy(**policy_values),
        ledger_path=ledger,
        clock=lambda: when,
    )
    barrier.wait()
    result = usage.admit(request(identifier))
    results.put("admitted" if isinstance(result, Admission) else result.reason)


def _run_admission_race(ledger, policy, when, count=6):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(count)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_admit,
            args=(ledger, policy, when, barrier, results, f"racer-{index}"),
        )
        for index in range(count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    return [results.get(timeout=2) for _ in processes]


def _reserve_and_exit(ledger, identifier, child_ready=None):
    usage = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=ledger,
    )
    admitted = usage.admit(request(identifier))
    if child_ready is None:
        return
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    admitted.mark_started(pid=child.pid, pgid=child.pid)
    child_ready.put(child.pid)


def _reserve_finished_native_and_exit(ledger, identifier):
    usage = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=ledger,
    )
    admitted = usage.admit(request(identifier, selected=route(client="codex")))
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
    )
    admitted.mark_started(pid=child.pid, pgid=child.pid)
    child.wait()


def _wait_for_process_exit(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.exists():
            fields = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if fields and fields[0] == "Z":
                return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} did not exit")


def test_slots_hold_across_processes(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    policy = {
        "max_concurrent": 2,
        "max_runs_per_day": 50,
        "max_turns_per_run": 60,
        "min_seconds_between_runs": 1,
    }
    usage = Governor(
        Policy(**policy),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
    )
    held = usage.admit(request("held"))
    held.mark_started(pid=os.getpid(), pgid=os.getpgrp())
    clock.advance(2)

    results = _run_admission_race(
        usage.ledger_path,
        policy,
        clock(),
    )

    assert results.count("admitted") == 1
    assert results.count("concurrency") == 5


def test_last_daily_admission_race(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    policy = {
        "max_concurrent": 8,
        "max_runs_per_day": 2,
        "max_turns_per_run": 60,
        "min_seconds_between_runs": 1,
    }
    usage = Governor(
        Policy(**policy),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
    )
    finish(usage.admit(request("completed")))
    clock.advance(2)

    results = _run_admission_race(
        usage.ledger_path,
        policy,
        clock(),
    )

    assert results.count("admitted") == 1
    assert results.count("daily_limit") == 5


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
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    assert Policy.load() == Policy()
    assert Governor().ledger_path == state_home / "magnolia" / "usage-ledger.jsonl"
    with pytest.raises(PolicyError):
        Policy.load(tmp_path / "missing.yaml")
    policy_path = config_home / "magnolia" / "usage-policy.yaml"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("max_concurrent: 0\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        Policy.load()

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert Policy.load() == Policy()
    assert Governor().ledger_path == tmp_path / "home" / ".local/state/magnolia/usage-ledger.jsonl"

    default_policy = tmp_path / "home" / ".config/magnolia/usage-policy.yaml"
    default_policy.parent.mkdir(parents=True)
    default_policy.symlink_to(tmp_path / "missing-policy.yaml")
    with pytest.raises(PolicyError, match="dangling"):
        Policy.load()


def test_policy_has_no_legacy_max_price_per_run_field():
    with pytest.raises(TypeError, match="max_price_per_run"):
        Policy(max_price_per_run=1.0)


@pytest.mark.parametrize("kind", ["failure", "timeout", "cancelled"])
def test_release_idempotent_on_exception_timeout_cancel(tmp_path, kind):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    admitted = usage.admit(request(kind))
    if kind == "failure":
        with pytest.raises(RuntimeError, match="boom"):
            with admitted as active:
                active.mark_started(pid=101, pgid=101, process_start_token="proc-1")
                raise RuntimeError("boom")
    else:
        with admitted as active:
            active.mark_started(pid=101, pgid=101, process_start_token="proc-1")
            active.release(outcome=RunOutcome(kind))
    admitted.release(outcome=RunOutcome("success"))
    rows = (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["outcome"]["kind"] == kind


def test_dead_owner_live_child_not_reclaimed(tmp_path):
    context = multiprocessing.get_context("spawn")
    child_ready = context.Queue()
    ledger = tmp_path / "usage-ledger.jsonl"
    owner = context.Process(target=_reserve_and_exit, args=(ledger, "one", child_ready))
    owner.start()
    child_pid = child_ready.get(timeout=5)
    owner.join(5)
    assert owner.exitcode == 0
    usage = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=ledger,
    )
    try:
        assert usage.reconcile("one", service_request_ended=True) is False
        assert usage.admit(request("two")).reason == "concurrency"
    finally:
        os.killpg(child_pid, signal.SIGTERM)
        _wait_for_process_exit(child_pid)
    assert usage.reconcile("one", service_request_ended=True) is True


def test_reconcile_is_environment_independent_and_unknown_is_alive(
    tmp_path, monkeypatch
):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    usage = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
    )
    usage.admit(request("one")).mark_started(pid=child.pid, pgid=child.pid)
    try:
        monkeypatch.setenv("TZ", "UTC")
        monkeypatch.setenv("LANG", "fr_FR.UTF-8")
        monkeypatch.setenv("PATH", "/nonexistent")
        assert not usage.reconcile("one", service_request_ended=True)
        monkeypatch.setattr(
            governor_module, "_verified_process_start_token", lambda _pid: None
        )
        assert not usage.reconcile("one", service_request_ended=True)
    finally:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)


def test_macos_start_token_uses_absolute_ps_and_pinned_environment(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Sun Nov 15 22:25:16 2026\n",
            stderr="",
        )

    monkeypatch.setattr(
        process_identity.Path,
        "read_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    token = governor_module._verified_process_start_token(123)

    assert token == "ps:123:Sun Nov 15 22:25:16 2026"
    argv, kwargs = calls[0]
    assert argv[0] == "/bin/ps"
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["env"]["TZ"] == "UTC"


def test_unparsable_start_token_is_unknown(monkeypatch):
    monkeypatch.setattr(
        process_identity.Path,
        "read_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="not a timestamp\n", stderr=""
        ),
    )
    assert governor_module._verified_process_start_token(123) is None


def test_proc_start_token_parses_bytes_after_last_parenthesis(monkeypatch):
    fields = [b"S", b"1", b"4242", *([b"1"] * 16), b"987654"]
    body = b"4242 (invalid-\xff) comm ) " + b" ".join(fields) + b"\n"
    monkeypatch.setattr(process_identity.Path, "read_bytes", lambda *_a, **_k: body)

    assert governor_module._verified_process_start_token(4242) == "proc:4242:987654"


def test_unparsable_proc_state_is_unknown_and_therefore_alive(monkeypatch):
    monkeypatch.setattr(
        process_identity.Path,
        "read_bytes",
        lambda *_a, **_k: b"malformed proc stat without a closing delimiter",
    )
    monkeypatch.setattr(governor_module.os, "kill", lambda _pid, _signal: None)

    reservation = {"pid": 4242, "pgid": 4242, "process_start_token": "proc:4242:1"}

    assert governor_module._verified_process_start_token(4242) is None
    assert governor_module._reservation_process_alive(reservation)


@pytest.mark.parametrize(
    ("has_live_members", "expected"),
    [(False, False), (True, True), (None, True)],
)
def test_eperm_group_uses_membership_probe(
    monkeypatch, has_live_members, expected
):
    def deny_group_probe(_pgid, _signal):
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(governor_module.os, "killpg", deny_group_probe)
    monkeypatch.setattr(governor_module.Path, "is_dir", lambda *_a, **_k: False)
    monkeypatch.setattr(
        governor_module,
        "_ps_group_has_live_members",
        lambda _pgid: has_live_members,
    )

    assert governor_module._process_group_exists(4242) is expected


def test_zombie_leader_with_empty_group_is_gone():
    leader = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    token = governor_module._verified_process_start_token(leader.pid)
    deadline = time.monotonic() + 5
    try:
        state = governor_module._process_state(leader.pid)
        while state is not None and "Z" not in state:
            if time.monotonic() >= deadline:
                pytest.fail(f"process {leader.pid} did not become a zombie")
            time.sleep(0.01)
            state = governor_module._process_state(leader.pid)
        if state is None:
            pytest.skip("process state is unavailable in this sandbox")
        reservation = {
            "pid": leader.pid,
            "pgid": leader.pid,
            "process_start_token": token,
        }
        assert not governor_module._reservation_process_alive(reservation)
    finally:
        leader.wait(timeout=5)


def test_macos_zombie_state_and_empty_group_use_pinned_ps(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = "Z+\n" if argv[1:3] == ["-o", "stat="] else "4242 Z+\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(
        governor_module.Path,
        "read_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(governor_module.Path, "is_dir", lambda *_a, **_k: False)
    monkeypatch.setattr(governor_module.subprocess, "run", fake_run)
    monkeypatch.setattr(governor_module.os, "killpg", lambda _pgid, _signal: None)

    assert governor_module._process_state(4242) == "Z+"
    assert not governor_module._process_group_exists(4242)
    assert all(call[1]["env"] == {"LC_ALL": "C", "TZ": "UTC"} for call in calls)


def test_dead_leader_with_live_group_member_is_retained_then_reclaimed(tmp_path):
    child_pid_path = tmp_path / "group-child.pid"
    release_path = tmp_path / "release-leader"
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import subprocess, sys, time; "
                f"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                f"Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
                f"release=Path({str(release_path)!r}); "
                "\nwhile not release.exists(): time.sleep(0.01)"
            ),
        ],
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_path.exists()
    usage = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
    )
    usage.admit(request("one")).mark_started(pid=leader.pid, pgid=leader.pid)
    release_path.touch()
    leader.wait(timeout=5)

    try:
        assert not usage.reconcile("one", service_request_ended=True)
    finally:
        os.killpg(leader.pid, signal.SIGTERM)
        _wait_for_process_exit(int(child_pid_path.read_text(encoding="utf-8")))
    assert usage.reconcile("one", service_request_ended=True)


def test_never_launched_reservation_owner_lifecycle(tmp_path):
    usage = governor(
        tmp_path,
        FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc)),
        max_concurrent=1,
        min_seconds_between_runs=1,
    )
    usage.admit(request("live-owner"))
    assert not usage.reconcile(
        "live-owner", process_ended=True, service_request_ended=True
    )

    context = multiprocessing.get_context("spawn")
    dead_owner_dir = tmp_path / "dead-owner"
    dead_owner_dir.mkdir()
    dead_owner_ledger = dead_owner_dir / "usage-ledger.jsonl"
    dead_owner = context.Process(
        target=_reserve_and_exit,
        args=(dead_owner_ledger, "dead-owner"),
    )
    dead_owner.start()
    dead_owner.join(5)
    assert dead_owner.exitcode == 0
    abandoned = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=dead_owner_ledger,
    )
    assert abandoned.reconcile(
        "dead-owner", process_ended=False, service_request_ended=False
    )
    assert isinstance(abandoned.admit(request("replacement")), Admission)


def test_admission_reconciles_dead_never_launched_owner(tmp_path):
    context = multiprocessing.get_context("spawn")
    ledger = tmp_path / "usage-ledger.jsonl"
    dead_owner = context.Process(
        target=_reserve_and_exit,
        args=(ledger, "dead-owner"),
    )
    dead_owner.start()
    dead_owner.join(5)
    assert dead_owner.exitcode == 0

    usage = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=ledger,
    )

    assert isinstance(usage.admit(request("replacement")), Admission)


def test_admission_finalizes_ended_abandoned_native_invocation(tmp_path):
    context = multiprocessing.get_context("spawn")
    ledger = tmp_path / "usage-ledger.jsonl"
    dead_owner = context.Process(
        target=_reserve_finished_native_and_exit,
        args=(ledger, "abandoned-native"),
    )
    dead_owner.start()
    dead_owner.join(5)
    assert dead_owner.exitcode == 0

    usage = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=ledger,
    )
    replacement = usage.admit(
        request("replacement", selected=route(client="codex"))
    )

    assert isinstance(replacement, (Admission, QuotaWaiting))
    if isinstance(replacement, QuotaWaiting):
        assert replacement.reason == "spacing"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert rows[0]["invocation_id"] == "abandoned-native"
    assert rows[0]["outcome"]["kind"] == "failure"


def test_process_start_error_cancels_before_client_exec(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    admission = usage.admit(request("pre-exec"))
    admission.mark_started(pid=202, pgid=202, process_start_token="birth")

    with pytest.raises(ProcessStartError):
        with admission:
            raise ProcessStartError(errno.EIO, "barrier failed")

    assert not usage.ledger_path.exists()
    assert usage._load_state()["reservations"] == {}


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

    usage.resume_key("codex")
    usage.resume_key("codex")
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


def test_ledger_carries_h1_cache_fields_when_reported(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    record = UsageRecord(
        input_tokens=100,
        uncached_input_tokens=25,
        output_tokens=10,
        cached_tokens=75,
        cache_write_tokens=8,
    )

    finish(usage.admit(request("h1")), usage=record)

    ledger = json.loads((tmp_path / "usage-ledger.jsonl").read_text())
    assert ledger["usage"]["uncached_input_tokens"] == 25
    assert ledger["usage"]["cache_read_tokens"] == 75
    assert ledger["usage"]["cache_creation_tokens"] == 8


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


def test_report_does_not_repair_complete_unterminated_ledger_row(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    row = {
        "invocation_id": "one",
        "reserved_at": clock().isoformat(),
        "subscription_key": "codex",
        "project_digest": "project-digest",
        "usage": None,
    }
    original = json.dumps(row, sort_keys=True).encode()
    usage.ledger_path.write_bytes(original)

    assert usage.report(clock().date())["day"]["runs"] == 1
    assert usage.ledger_path.read_bytes() == original


def test_cancelled_and_stale_waits_are_not_active(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, max_concurrent=1, min_seconds_between_runs=1)
    cancelled = usage.admit(
        request("cancelled"),
        type("Control", (), {"should_stop": lambda self: "stop"})(),
    )
    assert cancelled.reason == "cancelled"
    assert usage.report(clock().date())["waits"] == []

    usage.admit(request("held"))
    context = multiprocessing.get_context("spawn")
    waiter_barrier = context.Barrier(1)
    waiter_results = context.Queue()
    waiter = context.Process(
        target=_race_admit,
        args=(
            usage.ledger_path,
            {
                "max_concurrent": 1,
                "max_runs_per_day": 50,
                "max_turns_per_run": 60,
                "min_seconds_between_runs": 1,
            },
            clock(),
            waiter_barrier,
            waiter_results,
            "gone-waiter",
        ),
    )
    waiter.start()
    waiter.join(5)
    assert waiter.exitcode == 0
    assert usage.report(clock().date())["waits"] == []


def test_ledger_has_no_prompt_reply_or_key(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    from campaign_runner.workers import InvocationGovernance, OpenCodeExecutor

    binary = tmp_path / "privacy-client"
    binary.write_text(
        """#!/usr/bin/env python3
import json
print(json.dumps({'sessionID': 'private-session', 'part': {'type': 'text', 'messageID': 'm1', 'text': 'reply-sentinel-7e8f'}}))
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    executor = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
        environment={"PROVIDER_API_KEY": "key-sentinel-4d5c"},
        governance=InvocationGovernance(
            usage,
            route(),
            "private-run",
            "project-digest",
        ),
    )
    executor.run("prompt-sentinel-1a2b", tmp_path, tmp_path / "privacy-attempt")
    body = (tmp_path / "usage-ledger.jsonl").read_text()
    assert "prompt-sentinel-1a2b" not in body
    assert "reply-sentinel-7e8f" not in body
    assert "key-sentinel-4d5c" not in body


def test_unknown_quota_requires_explicit_resume(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    admitted = usage.admit(request("one"))
    finish(admitted, outcome="quota")
    clock.advance(86400)
    assert usage.admit(request("two")).reason == "quota"
    assert usage.resume_key("opencode:anthropic")
    assert isinstance(usage.admit(request("three")), Admission)


def test_resume_compare_and_clear_and_conservative_hold_merge(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(
        tmp_path,
        clock,
        max_concurrent=3,
        min_seconds_between_runs=1,
    )
    first = usage.admit(request("first"))
    clock.advance(1)
    second = usage.admit(request("second"))
    clock.advance(1)
    third = usage.admit(request("third"))
    first.mark_started(pid=1, pgid=1, process_start_token="first")
    first.release(
        outcome=RunOutcome(
            "quota",
            LimitInfo("first", clock() + timedelta(hours=2), "probe"),
        )
    )
    observed = usage.report(clock().date())["holds"][0]

    second.mark_started(pid=2, pgid=2, process_start_token="second")
    second.release(
        outcome=RunOutcome(
            "quota",
            LimitInfo("second", clock() + timedelta(hours=1), "probe"),
        )
    )
    current = usage.report(clock().date())["holds"][0]
    assert current["generation"] != observed["generation"]
    assert current["reset_at"] == observed["reset_at"]
    assert not usage.resume_key(
        "opencode:anthropic", expected_hold=observed["generation"]
    )

    third.mark_started(pid=3, pgid=3, process_start_token="third")
    third.release(outcome=RunOutcome("quota"))
    unknown = usage.report(clock().date())["holds"][0]
    assert unknown["reset_at"] is None
    assert usage.resume_key(
        "opencode:anthropic", expected_hold=unknown["generation"]
    )


def test_torn_final_ledger_line_is_quarantined_other_corruption_refuses(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
    usage = governor(tmp_path, clock, min_seconds_between_runs=1)
    finish(usage.admit(request("one")))
    clock.advance(2)
    with usage.ledger_path.open("ab") as handle:
        handle.write(b'{"invocation_id":"torn"')

    assert isinstance(usage.admit(request("two")), Admission)
    quarantine = usage.ledger_path.with_suffix(".jsonl.quarantine")
    assert quarantine.exists()
    assert "torn" in quarantine.read_text(encoding="utf-8")

    corrupt = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "corrupt.jsonl",
        clock=clock,
    )
    corrupt.ledger_path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid usage ledger row 1"):
        corrupt.admit(request("refused"))


def test_daily_accounting_uses_reservation_day(tmp_path):
    clock = FakeClock(datetime(2026, 11, 15, 23, 59, 59, tzinfo=timezone.utc))
    usage = governor(
        tmp_path,
        clock,
        max_runs_per_day=1,
        min_seconds_between_runs=1,
    )
    admitted = usage.admit(request("before-midnight"))
    clock.advance(2)
    finish(admitted)
    clock.advance(2)

    assert isinstance(usage.admit(request("new-day")), Admission)
