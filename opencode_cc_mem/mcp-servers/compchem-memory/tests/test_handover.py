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


# ---- generate_handover ------------------------------------------------------

def _write_mapping(store, ids):
    p = store / "opencode-sessions.jsonl"
    p.write_text("".join(json.dumps({"opencode_session_id": i, "ts": str(n)}) + "\n"
                         for n, i in enumerate(ids)))


def _msg(mid, role, text):
    return {"info": {"id": mid, "role": role}, "parts": [{"type": "text", "text": text}]}


def test_generate_first_run_writes_state_and_cursor(store):
    _write_mapping(store, ["ses_a"])
    export = {"info": {"id": "ses_a"},
              "messages": [_msg("m1", "user", "dock KILDQ"), _msg("m2", "assistant", "cluster 2 best")]}
    captured = {}

    def fake_llm(system, user, max_tokens=2000, **kw):
        captured["user"] = user
        return "## Done\n- docked KILDQ, cluster 2 best\n"

    path = hv.generate_handover(str(store.parent), exporter=lambda s: export, llm=fake_llm)
    assert path is not None
    assert "docked KILDQ" in (store / hv.HANDOVER_STATE_FILE).read_text()
    assert "dock KILDQ" in captured["user"]          # transcript fed to LLM
    assert "(none yet" in captured["user"]            # empty base announced
    cur = json.loads((store / hv.HANDOVER_CURSOR_FILE).read_text())
    assert cur["sid"] == "ses_a" and cur["cursor"] == "m2"


def test_generate_no_new_messages_is_noop(store):
    _write_mapping(store, ["ses_a"])
    export = {"info": {"id": "ses_a"}, "messages": [_msg("m1", "user", "hi")]}
    (store / hv.HANDOVER_CURSOR_FILE).write_text(json.dumps({"sid": "ses_a", "cursor": "m1"}))
    called = {"llm": False}

    def fake_llm(*a, **k):
        called["llm"] = True
        return "x"

    assert hv.generate_handover(str(store.parent), exporter=lambda s: export, llm=fake_llm) is None
    assert called["llm"] is False                    # never reached the LLM
    assert not (store / hv.HANDOVER_STATE_FILE).exists()


def test_generate_llm_failure_leaves_state_untouched(store):
    _write_mapping(store, ["ses_a"])
    (store / hv.HANDOVER_STATE_FILE).write_text("## Done\n- prior\n")
    export = {"info": {"id": "ses_a"}, "messages": [_msg("m1", "user", "new work")]}

    assert hv.generate_handover(str(store.parent), exporter=lambda s: export, llm=lambda *a, **k: None) is None
    assert (store / hv.HANDOVER_STATE_FILE).read_text() == "## Done\n- prior\n"   # unchanged
    assert not (store / hv.HANDOVER_CURSOR_FILE).exists()                          # cursor not advanced


def test_generate_export_failure_is_noop(store):
    _write_mapping(store, ["ses_a"])
    assert hv.generate_handover(str(store.parent), exporter=lambda s: None, llm=lambda *a, **k: "x") is None


def test_generate_no_mapping_is_noop(store):
    assert hv.generate_handover(str(store.parent), exporter=lambda s: {}, llm=lambda *a, **k: "x") is None
