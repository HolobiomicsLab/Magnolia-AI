import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.executors.contracts import SessionIdentity  # noqa: E402
from campaign_runner.state import (  # noqa: E402
    CampaignState,
    CampaignStore,
    QuotaWait,
    StateError,
)


NOW = datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc)


def make_store(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    protocol = tmp_path / "protocol.json"
    protocol.write_text(
        json.dumps(
            {
                "schema": 1,
                "id": "quota-pilot",
                "project": "project",
                "worker": {
                    "model": "opencode-go/deepseek-v4.1-flash",
                    "agent": "campaign-worker",
                },
                "reviewer": {"enabled": True, "model": "opencode-go/kimi-k3.1"},
                "budget": {
                    "max_tokens": 1000,
                    "max_wall_hours": 1,
                    "max_core_hours": 1,
                    "max_attempts_per_stage": 2,
                    "max_parallel_jobs": 1,
                    "max_followups": 0,
                },
                "stages": [
                    {
                        "id": "analyze",
                        "type": "opencode",
                        "prompt": "prompts/analyze.md",
                        "gate": ["/bin/true"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Approved brief\n", encoding="utf-8")
    store = CampaignStore(tmp_path / "project" / "runs" / "run")
    store.initialize(protocol, brief)
    return store


def quota_wait(
    phase="pre-worker",
    *,
    reset_at=None,
    attempt=0,
    result_ref=None,
    retry_cursor=None,
    hold_id="hold-1",
):
    return QuotaWait(
        reason="subscription quota",
        subscription_key="opencode:anthropic",
        observed_at=NOW,
        hold_id=hold_id,
        reset_at=reset_at,
        suspended_phase=phase,
        stage="analyze",
        attempt=attempt,
        session_identity=SessionIdentity(
            "opencode",
            "anthropic/claude-sonnet-4-5",
            "session-1",
            "project-digest",
            "reviewer" if phase == "reviewer" else "worker",
            "policy-digest",
        ),
        result_ref=result_ref,
        retry_cursor=retry_cursor,
    )


def test_quota_wait_roundtrip_and_legacy_state(tmp_path):
    store = make_store(tmp_path)
    waiting = store.wait_for_quota(quota_wait(reset_at=NOW + timedelta(hours=1)))

    loaded = store.load()
    assert loaded == waiting
    assert loaded.quota_wait == quota_wait(reset_at=NOW + timedelta(hours=1))
    assert loaded.to_dict()["quota_wait"]["observed_at"] == NOW.isoformat()

    legacy = loaded.to_dict()
    legacy["state"] = "queued"
    legacy.pop("quota_wait")
    assert CampaignState.from_dict(legacy).quota_wait is None

    malformed = loaded.to_dict()
    malformed["quota_wait"]["surprise"] = True
    with pytest.raises(StateError, match="unknown fields"):
        CampaignState.from_dict(malformed)


def test_reconcile_restores_phase_result_and_reset(tmp_path):
    store = make_store(tmp_path)
    store.transition("running", "worker started", stage="analyze", attempt=1)
    store.transition("validating", "worker succeeded", attempt=1)
    checkpoint = quota_wait(
        "reviewer",
        reset_at=NOW + timedelta(hours=2),
        attempt=1,
        result_ref="attempts/analyze/1/worker_result.json",
        retry_cursor=2,
    )
    store.wait_for_quota(checkpoint)
    store.campaign_path.unlink()

    repaired = store.reconcile()

    assert repaired.state == "waiting_quota"
    assert repaired.quota_wait == checkpoint
    assert repaired.quota_wait.result_ref.endswith("worker_result.json")
    assert repaired.quota_wait.retry_cursor == 2


def test_prelaunch_wait_does_not_increment_attempt(tmp_path):
    store = make_store(tmp_path)

    waiting = store.wait_for_quota(quota_wait("pre-worker", attempt=0))

    assert waiting.state == "waiting_quota"
    assert waiting.attempt == 0
    assert waiting.quota_wait.attempt == 0
    assert store.read_events()[-1]["attempt"] == 0


def test_worker_and_validating_reviewer_can_wait(tmp_path):
    worker_store = make_store(tmp_path / "worker")
    worker_store.transition("running", "worker started", stage="analyze", attempt=1)
    worker = worker_store.wait_for_quota(quota_wait("worker", attempt=1))
    assert worker.quota_wait.suspended_phase == "worker"

    reviewer_store = make_store(tmp_path / "reviewer")
    reviewer_store.transition("running", "worker started", stage="analyze", attempt=1)
    reviewer_store.transition("validating", "worker succeeded", attempt=1)
    reviewer = reviewer_store.wait_for_quota(
        quota_wait(
            "reviewer",
            attempt=1,
            result_ref="attempts/analyze/1/worker_result.json",
            retry_cursor=1,
        )
    )
    assert reviewer.quota_wait.suspended_phase == "reviewer"
    assert reviewer.quota_wait.result_ref.endswith("worker_result.json")
    assert reviewer.attempt == 1


def test_no_reset_stays_waiting(tmp_path):
    store = make_store(tmp_path)
    store.wait_for_quota(quota_wait())

    unchanged = store.resume_if_quota_ready(NOW + timedelta(days=30))

    assert unchanged.state == "waiting_quota"
    assert unchanged.quota_wait.hold_id == "hold-1"


def test_known_reset_and_manual_resume_restore_phase(tmp_path):
    timed = make_store(tmp_path / "timed")
    reset_at = NOW + timedelta(hours=1)
    timed.wait_for_quota(quota_wait(reset_at=reset_at))
    assert timed.resume_if_quota_ready(reset_at - timedelta(seconds=1)).state == "waiting_quota"
    resumed = timed.resume_if_quota_ready(reset_at)
    assert resumed.state == "queued"
    assert resumed.attempt == 0
    assert resumed.quota_wait is None
    assert resumed.resumed_from_quota == quota_wait(reset_at=reset_at)

    manual = make_store(tmp_path / "manual")
    manual.transition("running", "worker started", stage="analyze", attempt=1)
    checkpoint = quota_wait("worker", attempt=1, hold_id="fresh-hold")
    manual.wait_for_quota(checkpoint)
    resumed = manual.resume_if_quota_ready(
        NOW, manual=True, expected_hold_id="fresh-hold"
    )
    assert resumed.state == "running"
    assert resumed.attempt == 1
    assert resumed.quota_wait is None
    assert resumed.resumed_from_quota == checkpoint
    assert manual.read_events()[-1]["quota_hold_id"] == "fresh-hold"


def test_manual_resume_requires_current_hold_generation(tmp_path):
    store = make_store(tmp_path)
    first = quota_wait(hold_id="h1", reset_at=NOW)
    store.wait_for_quota(first)
    assert store.resume_if_quota_ready(NOW).resumed_from_quota == first

    second = quota_wait(hold_id="h2")
    store.wait_for_quota(second)
    with pytest.raises(StateError, match="expected_hold_id"):
        store.resume_if_quota_ready(NOW, manual=True)

    stale = store.resume_if_quota_ready(
        NOW, manual=True, expected_hold_id="h1"
    )
    assert stale.state == "waiting_quota"
    assert stale.quota_wait == second
    assert stale.resumed_from_quota is None

    resumed = store.resume_if_quota_ready(
        NOW, manual=True, expected_hold_id="h2"
    )
    assert resumed.state == "queued"
    assert resumed.resumed_from_quota == second


def test_resume_crash_reconciles_continuation_data(tmp_path):
    store = make_store(tmp_path)
    store.transition("running", "worker started", stage="analyze", attempt=1)
    store.transition("validating", "worker succeeded", attempt=1)
    checkpoint = quota_wait(
        "reviewer",
        reset_at=NOW,
        attempt=1,
        result_ref="attempts/analyze/1/worker_result.json",
        retry_cursor=2,
    )
    store.wait_for_quota(checkpoint)
    real_save = store.save
    store.save = lambda state: (_ for _ in ()).throw(OSError("crash"))
    with pytest.raises(OSError, match="crash"):
        store.resume_if_quota_ready(NOW)
    store.save = real_save

    repaired = store.reconcile()

    assert repaired.state == "validating"
    assert repaired.quota_wait is None
    assert repaired.resumed_from_quota == checkpoint
    assert repaired.resumed_from_quota.result_ref.endswith("worker_result.json")
    assert repaired.resumed_from_quota.retry_cursor == 2
    assert repaired.resumed_from_quota.session_identity == checkpoint.session_identity
    assert store.load().resumed_from_quota == checkpoint
    assert store.read_events()[-1]["quota_resumed_from"] == checkpoint.to_dict()

    advanced = store.transition("queued", "review retry completed", attempt=1)
    assert advanced.resumed_from_quota is None
    assert store.read_events()[-1]["quota_resumed_from"] is None


def test_paused_quota_exit_clears_checkpoint_and_cannot_reenter_generically(tmp_path):
    store = make_store(tmp_path)
    store.wait_for_quota(quota_wait(hold_id="old"))
    store.pause("operator pause")

    queued = store.transition("queued", "operator requeue")

    assert queued.quota_wait is None
    assert store.read_events()[-1]["quota_wait_after"] is None
    store.transition("running", "worker started", attempt=1)
    with pytest.raises(StateError, match="wait_for_quota"):
        store.transition("waiting_quota", "generic entry")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("stage", "no-such-stage", "quota checkpoint stage"),
        ("attempt", 1, "quota checkpoint attempt"),
    ],
)
def test_load_rejects_checkpoint_inconsistent_with_campaign(
    tmp_path, field, value, message
):
    store = make_store(tmp_path)
    store.wait_for_quota(quota_wait())
    payload = json.loads(store.campaign_path.read_text())
    payload["quota_wait"][field] = value
    store.campaign_path.write_text(json.dumps(payload))

    with pytest.raises(StateError, match=message):
        store.load()


def test_pause_halt_stop_while_waiting(tmp_path):
    paused_store = make_store(tmp_path / "paused")
    paused_store.wait_for_quota(quota_wait())
    paused = paused_store.pause("operator pause")
    assert paused.state == "paused"
    assert paused.quota_wait.hold_id == "hold-1"
    restored = paused_store.resume("operator resume")
    assert restored.state == "waiting_quota"
    assert restored.quota_wait.hold_id == "hold-1"

    halted_store = make_store(tmp_path / "halted")
    halted_store.wait_for_quota(quota_wait())
    halted = halted_store.halt("operator stop")
    assert halted.state == "halted"
    assert halted.quota_wait.hold_id == "hold-1"

    failed_store = make_store(tmp_path / "failed")
    failed_store.wait_for_quota(quota_wait())
    failed = failed_store.fail("stop failed")
    assert failed.state == "failed"
    assert failed.quota_wait.hold_id == "hold-1"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("reset_at", datetime(2026, 11, 16), "timezone-aware"),
        ("retry_cursor", -1, "retry_cursor"),
        ("result_ref", "", "result_ref"),
    ],
)
def test_quota_wait_optional_fields_are_validated(field, value, message):
    values = quota_wait().to_dict()
    values[field] = value
    with pytest.raises((StateError, ValueError), match=message):
        QuotaWait.from_dict(values)
