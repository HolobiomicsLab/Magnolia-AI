"""The MCP server must survive a client disconnecting mid-request.

Background: when opencode aborts/cancels a tool call (or the connection drops)
while a tool is still running, the tool's response is later written to a stream
the peer has already closed. anyio raises ClosedResourceError deep inside the
MCP request TaskGroup; unhandled, it propagates as an ExceptionGroup out of
mcp.run() and kills the whole process — taking the background job poller (a
daemon thread) down with it. Reproduced live 2026-06-17 (run f4068d23): an
abort at 13:13:07 -> server death at 13:14:18.

run_shell hardening (test_run_shell.py) does NOT cover this: the failure is in
the transport writing the response, not in the tool. These tests pin the
serve-loop's resilience contract: swallow disconnect-shaped exception groups,
re-raise everything else.
"""
import anyio
import pytest

from compchem_tools.server import _is_client_disconnect, _serve_resilient


# ── _is_client_disconnect ────────────────────────────────────────────────────


def test_pure_disconnect_group_is_benign():
    eg = ExceptionGroup("transport", [anyio.ClosedResourceError()])
    assert _is_client_disconnect(eg) is True


def test_nested_disconnect_group_is_benign():
    inner = ExceptionGroup("inner", [anyio.BrokenResourceError()])
    outer = ExceptionGroup("outer", [inner])
    assert _is_client_disconnect(outer) is True


def test_mixed_group_is_not_benign():
    """A group containing ANY real error must still crash loudly."""
    eg = ExceptionGroup("mixed", [anyio.ClosedResourceError(), ValueError("real bug")])
    assert _is_client_disconnect(eg) is False


def test_plain_real_exception_is_not_benign():
    assert _is_client_disconnect(ValueError("boom")) is False


# ── _serve_resilient ─────────────────────────────────────────────────────────


def test_clean_shutdown_runs_once():
    calls = {"n": 0}

    def run_fn():
        calls["n"] += 1  # returns normally -> clean stdin EOF

    _serve_resilient(run_fn=run_fn)
    assert calls["n"] == 1


def test_disconnect_does_not_propagate_and_stops_hot_loop():
    """A disconnect group must NOT escape; repeated fast failures must not hot-loop."""
    calls = {"n": 0}

    def run_fn():
        calls["n"] += 1
        raise ExceptionGroup("transport", [anyio.ClosedResourceError()])

    # Fake clock: every attempt is "fast" (<1s), so the consecutive-failure guard trips.
    ticks = iter(range(0, 1000))

    def fake_monotonic():
        return next(ticks) * 0.0  # always 0 -> elapsed 0 -> counts as fast failure

    _serve_resilient(run_fn=run_fn, monotonic=fake_monotonic)  # must return, not raise
    assert calls["n"] == 3  # stops after 3 consecutive fast disconnects


def test_real_exception_propagates():
    def run_fn():
        raise ExceptionGroup("oops", [ValueError("genuine bug")])

    with pytest.raises(BaseExceptionGroup):
        _serve_resilient(run_fn=run_fn)
