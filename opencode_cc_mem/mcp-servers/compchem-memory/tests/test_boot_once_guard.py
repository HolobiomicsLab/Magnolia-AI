"""Regression: server.py's module-level side effects (boot pipeline, distill
timer) must run once per PROCESS.

2026-09-17 incident: launched via ``python -m compchem_memory.server``, the
module exists in sys.modules only as ``__main__``; startup_scan's lazy
``from compchem_memory.server import RULES_DIR`` re-imported it under its
canonical name ~14 s into the boot, re-executing the module body and spawning
a SECOND overlapping boot pipeline (two handover merges of 83 s and 93 s, two
distill timers). A module-global flag cannot guard this — the two module
objects don't share globals — so the claim lives in the process environment,
keyed by project dir.
"""
import threading

import pytest


@pytest.fixture
def patched_server(monkeypatch, tmp_path):
    from compchem_memory import server

    monkeypatch.setattr(server, "PROJECT_DIR", str(tmp_path))
    return server


@pytest.fixture
def no_threads(monkeypatch):
    """Record Thread spawns without running them (the workers would boot-loop)."""
    spawned = []

    class _Recorder:
        def __init__(self, target=None, **kwargs):
            spawned.append((target, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", _Recorder)
    return spawned


def test_claim_once_first_call_wins(patched_server):
    assert patched_server._claim_once("boot") is True
    assert patched_server._claim_once("boot") is False


def test_claim_once_names_are_independent(patched_server):
    assert patched_server._claim_once("boot") is True
    assert patched_server._claim_once("distill_timer") is True


def test_claim_once_is_per_project(patched_server, monkeypatch, tmp_path):
    assert patched_server._claim_once("boot") is True
    monkeypatch.setattr(patched_server, "PROJECT_DIR", str(tmp_path / "other"))
    assert patched_server._claim_once("boot") is True


def test_boot_pipeline_spawns_once(patched_server, no_threads):
    patched_server._run_startup_scan_background()
    patched_server._run_startup_scan_background()
    assert len(no_threads) == 1


def test_distill_timer_spawns_once(patched_server, no_threads):
    patched_server._run_distill_timer_background()
    patched_server._run_distill_timer_background()
    assert len(no_threads) == 1


def test_boot_and_timer_each_spawn(patched_server, no_threads):
    patched_server._run_startup_scan_background()
    patched_server._run_distill_timer_background()
    assert len(no_threads) == 2


def test_boot_timing_never_writes_to_stdout(patched_server, monkeypatch, capsys):
    """Issue #21: in stdio mode stdout is the JSON-RPC channel. The boot worker
    must write [boot-timing] lines to stderr only, otherwise they corrupt the
    MCP stream (clients log 'Invalid JSON')."""
    monkeypatch.setattr(
        patched_server, "_build_boot_steps", lambda project_dir: [("noop", lambda: None)]
    )

    class _SyncThread:
        def __init__(self, target=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _SyncThread)

    patched_server._run_startup_scan_background()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[boot-timing]" in captured.err
