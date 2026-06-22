"""Rolling LLM handover: render view + state read + generate_handover merge."""

import json
from pathlib import Path

import pytest

from compchem_memory import handover as hv
from compchem_memory.storage import ensure_project_store


# ---- render_for_boot_context -----------------------------------------------

def test_render_strips_tombstones_keeps_stale():
    state = (
        "## Done\n- docked KILDQ\n\n"
        "## To do\n- query Perspicacite\n\n"
        "## Stale?\n- old idea (carried 5 sessions)\n\n"
        "## Won't-do / Archived\n- abandoned BindCraft route\n"
    )
    out = hv.render_for_boot_context(state)
    assert "docked KILDQ" in out
    assert "query Perspicacite" in out
    assert "old idea" in out                 # Stale? kept
    assert "abandoned BindCraft" not in out  # tombstone stripped
    assert "Won't-do" not in out


def test_render_no_tombstone_section_returns_all():
    state = "## Done\n- a\n\n## To do\n- b\n"
    out = hv.render_for_boot_context(state)
    assert "a" in out and "b" in out


# ---- read_handover_block ----------------------------------------------------

@pytest.fixture
def store(tmp_path):
    ensure_project_store(str(tmp_path))
    return Path(tmp_path) / ".magnolia"


def test_read_block_absent_returns_none(store):
    assert hv.read_handover_block(store) is None


def test_read_block_present_renders(store):
    (store / hv.HANDOVER_STATE_FILE).write_text(
        "## Done\n- x\n\n## Won't-do / Archived\n- gone\n"
    )
    block = hv.read_handover_block(store)
    assert block is not None
    assert "x" in block and "gone" not in block


def test_read_block_empty_file_returns_none(store):
    (store / hv.HANDOVER_STATE_FILE).write_text("   \n")
    assert hv.read_handover_block(store) is None
