"""Tests for the in-process distillation timer and its off-peak gate."""

import os
from datetime import datetime, timezone

import pytest


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_resolve_interval_default():
    from compchem_memory.server import _resolve_distill_interval_seconds
    os.environ.pop("MAGNOLIA_DISTILL_INTERVAL_MIN", None)
    assert _resolve_distill_interval_seconds() == 20 * 60


def test_resolve_interval_env_override(monkeypatch):
    from compchem_memory.server import _resolve_distill_interval_seconds
    monkeypatch.setenv("MAGNOLIA_DISTILL_INTERVAL_MIN", "5")
    assert _resolve_distill_interval_seconds() == 5 * 60


def test_resolve_interval_bad_env_falls_back_to_default(monkeypatch):
    from compchem_memory.server import _resolve_distill_interval_seconds
    monkeypatch.setenv("MAGNOLIA_DISTILL_INTERVAL_MIN", "not-a-number")
    assert _resolve_distill_interval_seconds() == 20 * 60


def test_timer_tick_swallows_exceptions(monkeypatch):
    """A failing sweep must not propagate — the timer thread must survive."""
    from compchem_memory import server

    def boom(_pd):
        raise RuntimeError("synthetic sweep failure")

    monkeypatch.setattr(server, "scan_and_distill", boom, raising=False)
    monkeypatch.setattr(server, "_is_peak_time", lambda *a, **kw: False, raising=False)
    # _distill_timer_tick wraps the sweep; it must not raise
    server._distill_timer_tick("/nonexistent/project")


# ============ off-peak gate =============

def test_peak_window_boundaries_weekday():
    from compchem_memory.server import _is_peak_time
    # Monday 2026-09-14
    assert _is_peak_time(_utc(2026, 9, 14, 1, 0)) is True     # first window opens
    assert _is_peak_time(_utc(2026, 9, 14, 0, 59)) is False
    assert _is_peak_time(_utc(2026, 9, 14, 2, 30)) is True
    assert _is_peak_time(_utc(2026, 9, 14, 4, 0)) is False    # end exclusive
    assert _is_peak_time(_utc(2026, 9, 14, 5, 0)) is False    # between windows
    assert _is_peak_time(_utc(2026, 9, 14, 6, 0)) is True     # second window
    assert _is_peak_time(_utc(2026, 9, 14, 9, 59)) is True
    assert _is_peak_time(_utc(2026, 9, 14, 10, 0)) is False


def test_weekend_is_never_peak():
    from compchem_memory.server import _is_peak_time
    assert _is_peak_time(_utc(2026, 9, 12, 2, 0)) is False    # Saturday
    assert _is_peak_time(_utc(2026, 9, 13, 9, 0)) is False    # Sunday


def test_tick_skips_sweep_during_peak(monkeypatch):
    from compchem_memory import server
    calls = []
    monkeypatch.setattr(server, "scan_and_distill",
                        lambda pd: calls.append(pd), raising=False)
    monkeypatch.setattr(server, "_is_peak_time", lambda *a, **kw: True, raising=False)
    server._distill_timer_tick("/p")
    assert calls == []


def test_tick_runs_sweep_offpeak(monkeypatch):
    from compchem_memory import server
    calls = []
    monkeypatch.setattr(server, "scan_and_distill",
                        lambda pd: calls.append(pd), raising=False)
    monkeypatch.setattr(server, "_is_peak_time", lambda *a, **kw: False, raising=False)
    server._distill_timer_tick("/p")
    assert calls == ["/p"]


def test_kill_switch_forces_sweep_during_peak(monkeypatch):
    from compchem_memory import server
    calls = []
    monkeypatch.setenv("MAGNOLIA_OFFPEAK_DISABLE", "1")
    monkeypatch.setattr(server, "scan_and_distill",
                        lambda pd: calls.append(pd), raising=False)
    monkeypatch.setattr(server, "_is_peak_time", lambda *a, **kw: True, raising=False)
    server._distill_timer_tick("/p")
    assert calls == ["/p"]


def test_offpeak_gate_disabled_env_values(monkeypatch):
    from compchem_memory.server import _offpeak_gate_disabled
    monkeypatch.delenv("MAGNOLIA_OFFPEAK_DISABLE", raising=False)
    assert _offpeak_gate_disabled() is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("MAGNOLIA_OFFPEAK_DISABLE", value)
        assert _offpeak_gate_disabled() is True
    monkeypatch.setenv("MAGNOLIA_OFFPEAK_DISABLE", "0")
    assert _offpeak_gate_disabled() is False
