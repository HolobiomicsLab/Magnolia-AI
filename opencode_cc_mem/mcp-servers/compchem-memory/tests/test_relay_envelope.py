"""Tests: the review relay envelope (blocking, once per project per day).

Background: pending-review notices rode as an advisory tail on successful
tool results and were never relayed to the user (8 deliveries, 0 relays in
260 replies — staging 20260930_153812). The envelope replaces the result of
the first read-only memory tool call of the day with a
``status: review_required`` payload — the anatomy of project_switch_blocked,
which agents relay reliably.
"""

import json

import pytest

from compchem_memory import distill_log
from compchem_memory.capture import (
    _attach_distill_notices,
    captured,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def project_dir(tmp_path):
    from compchem_memory.storage import ensure_project_store
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def _write_proposals(pd, kind="consolidation", n=3):
    reflex = pd / ".magnolia" / "reflex"
    reflex.mkdir(parents=True, exist_ok=True)
    name = ("consolidation-proposal.json" if kind == "consolidation"
            else "promotion-proposal.json")
    (reflex / name).write_text(json.dumps({
        "proposals": [{"title": f"p{i}"} for i in range(n)],
        "applied": [],
        "rejected": [],
        "dismissed": [],
    }))


def _latch_path(pd):
    return pd / ".magnolia" / "reflex" / ".relay-latch.json"


def test_envelope_fires_on_read_tool_when_pending(project_dir):
    _write_proposals(project_dir, "consolidation", n=9)
    distill_log.push_distill_notice(str(project_dir), "Q", "cleanup ready")

    result = _attach_distill_notices(
        json.dumps({"status": "ok"}), str(project_dir), "memory_get_context")

    parsed = json.loads(result)  # envelope must be valid JSON
    assert parsed["status"] == "review_required"
    assert parsed["pending"] == {"consolidation_proposals": 9}
    assert parsed["withheld_tool"] == "memory_get_context"
    assert "user" in parsed["message"].lower()
    # the queue is drained even on the envelope path
    assert distill_log.drain_distill_notices(str(project_dir)) == []


def test_envelope_latches_for_the_rest_of_the_day(project_dir):
    _write_proposals(project_dir, "consolidation", n=2)

    first = _attach_distill_notices(
        json.dumps({"status": "ok"}), str(project_dir), "memory_get_context")
    assert json.loads(first)["status"] == "review_required"

    distill_log.push_distill_notice(str(project_dir), "Q2", "nudge")
    second = _attach_distill_notices(
        json.dumps({"value": 1}), str(project_dir), "memory_search")

    # original result passes through whole; no envelope, no repeated tail
    assert json.loads(second) == {"value": 1}
    assert _latch_path(project_dir).exists()


def test_day_rollover_refires_envelope(project_dir, monkeypatch):
    from compchem_memory import capture
    _write_proposals(project_dir, "promotion", n=1)
    _latch_path(project_dir).parent.mkdir(parents=True, exist_ok=True)
    _latch_path(project_dir).write_text(json.dumps({"last_envelope": "2026-09-30"}))
    monkeypatch.setattr(capture, "_today", lambda: "2026-10-01")

    result = _attach_distill_notices(
        json.dumps({"status": "ok"}), str(project_dir), "memory_get_context")
    assert json.loads(result)["status"] == "review_required"
    assert json.loads(_latch_path(project_dir).read_text())["last_envelope"] == "2026-10-01"


def test_write_tool_never_withheld(project_dir):
    """Write tools must keep their real result (a withheld write result could
    cause a retry and a double write). Latch stays unset so the day's first
    read tool still fires the envelope."""
    _write_proposals(project_dir, "consolidation", n=4)
    distill_log.push_distill_notice(str(project_dir), "Q", "cleanup ready")

    result = _attach_distill_notices(
        json.dumps({"status": "created"}), str(project_dir), "memory_record_learning")

    parsed = json.loads(result)
    assert parsed["status"] == "created"  # real result survives
    assert parsed["_distill_notices"]    # legacy tail, not the envelope
    assert not _latch_path(project_dir).exists()

    envelope = _attach_distill_notices(
        json.dumps({"status": "ok"}), str(project_dir), "memory_get_context")
    assert json.loads(envelope)["status"] == "review_required"


def test_review_tool_itself_not_withheld(project_dir):
    """review_consolidation IS the relay action — withholding its result
    would hide the very proposals the agent must show."""
    _write_proposals(project_dir, "consolidation", n=1)
    result = _attach_distill_notices(
        json.dumps({"proposals": []}), str(project_dir), "memory_review_consolidation")
    assert json.loads(result) == {"proposals": []}


def test_no_pending_keeps_legacy_tail(project_dir):
    """Regression guard: with no pending proposals, behaviour is exactly the
    pre-envelope notice tail (test_distill_surfacing contract)."""
    distill_log.push_distill_notice(str(project_dir), "QUOTE-Z", "1 learning distilled")
    result = _attach_distill_notices(
        json.dumps({"success": True}), str(project_dir), "memory_get_context")
    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["_distill_notices"] == ["📝 1 learning distilled — QUOTE-Z"]
    assert not _latch_path(project_dir).exists()


def test_envelope_fires_through_captured_decorator(project_dir):
    _write_proposals(project_dir, "consolidation", n=2)

    @captured(source="compchem-memory")
    def memory_search(keyword="", project_dir: str | None = None) -> str:
        return json.dumps({"matches": []})

    result = memory_search(project_dir=str(project_dir))
    assert json.loads(result)["status"] == "review_required"


def test_latch_write_failure_falls_back_to_tail(project_dir, monkeypatch):
    """If the latch cannot be written, the envelope must not fire on every
    call (that would withhold every read result); the legacy tail surfaces
    the notices instead."""
    from compchem_memory import capture
    _write_proposals(project_dir, "consolidation", n=2)
    distill_log.push_distill_notice(str(project_dir), "Q", "cleanup ready")

    def _boom(*a, **k):
        return False
    monkeypatch.setattr(capture, "_mark_relay_latch", _boom)

    result = _attach_distill_notices(
        json.dumps({"status": "ok"}), str(project_dir), "memory_get_context")
    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["_distill_notices"]
