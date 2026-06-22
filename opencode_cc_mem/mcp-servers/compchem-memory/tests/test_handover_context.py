"""assemble_context session slot prefers the rolling handover, falls back to raw events."""

import json
from pathlib import Path

import pytest

from compchem_memory import context_assembly as ca
from compchem_memory import handover as hv
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def store(tmp_path):
    ensure_project_store(str(tmp_path))
    return Path(tmp_path) / ".magnolia"


def test_session_slot_uses_handover_when_present(store):
    (store / hv.HANDOVER_STATE_FILE).write_text("## Done\n- docked KILDQ\n")
    out = ca._get_session_context(store, budget=4000)
    assert out is not None
    assert "SESSION HANDOVER" in out
    assert "docked KILDQ" in out


def test_session_slot_falls_back_to_events_when_no_handover(store):
    (store / "sessions").mkdir(exist_ok=True)
    (store / "sessions" / "2026-06-22_000000.jsonl").write_text(
        json.dumps({"tool": "haddock3", "event_type": "tool_call"}) + "\n"
    )
    out = ca._get_session_context(store, budget=4000)
    assert out is not None
    assert "haddock3" in out          # raw-events fallback preserved


def test_session_slot_none_when_nothing(store):
    assert ca._get_session_context(store, budget=4000) is None
