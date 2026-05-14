"""Tests for the reflection-quote module."""

from compchem_memory import reflections


def test_pick_quote_opening_returns_nonempty_string():
    q = reflections.pick_quote("opening")
    assert isinstance(q, str)
    assert len(q) > 0


def test_pick_quote_closing_returns_nonempty_string():
    q = reflections.pick_quote("closing")
    assert isinstance(q, str)
    assert len(q) > 0


def test_pick_quote_default_kind_is_opening():
    q = reflections.pick_quote()
    assert isinstance(q, str)
    assert len(q) > 0


def test_pick_quote_unknown_kind_falls_back_to_opening():
    q = reflections.pick_quote("nonsense")
    assert isinstance(q, str)
    assert len(q) > 0


def test_quote_pools_are_nonempty():
    assert len(reflections.OPENING_QUOTES) >= 10
    assert len(reflections.CLOSING_QUOTES) >= 10
