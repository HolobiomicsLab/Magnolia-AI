"""Tests for the in-process distillation timer."""

import os

import pytest


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
    # _distill_timer_tick wraps the sweep; it must not raise
    server._distill_timer_tick("/nonexistent/project")
