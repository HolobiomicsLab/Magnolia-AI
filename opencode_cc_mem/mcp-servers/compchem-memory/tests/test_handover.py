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


# ---- budget_handover_block (section-aware, tail-preserving) ------------------

def _big_block(items: int = 6) -> str:
    done = "\n".join(
        f"- item{i}: " + "x" * 200 + f" (result {i})" for i in range(1, items + 1)
    )
    return (
        f"## Done\n{done}\n\n"
        "## In progress\n- actively tuning fle_sta_2 for the 6-mer\n\n"
        "## To do\n- run caprieval on cluster2\n\n"
        "## Key files\n- runs/2026-06-15_kferq/\n"
    )


def test_budget_block_passthrough_under_budget():
    block = _big_block()
    assert hv.budget_handover_block(block, len(block) + 1) == block


def test_budget_block_keeps_priority_sections_and_newest_done():
    block = _big_block()
    out = hv.budget_handover_block(block, 800)
    assert "In progress" in out and "actively tuning fle_sta_2" in out
    assert "To do" in out and "caprieval" in out
    assert "Key files" in out
    assert "elided" in out               # elision marker present
    assert "item6" in out                # newest Done items survive
    assert "item5" in out
    assert "item1" not in out            # OLDEST dropped, not newest
    assert "item2" not in out


def test_budget_block_overflow_keeps_actionable_sections_whole():
    # 2026-09-16 regression: priority sections alone exceed the budget. The
    # old tail-slice kept the last N chars and cut mid-line ANYWHERE — a
    # restarted session lost its whole To do list from boot context. Now the
    # actionable sections survive with items whole, the reference sections
    # are dropped, and a pointer to the full state file is kept.
    item = "y" * 60
    block = (
        "## Done\n- old done thing\n\n"
        "## In progress\n" + "\n".join(f"- line{i}: {item}" for i in range(30)) + "\n\n"
        "## To do\n- final next step\n- second next step\n\n"
        "## Key files\n- runs/2026-06-15_kferq/\n"
    )
    out = hv.budget_handover_block(block, 320)
    assert out.startswith("## In progress")            # boundary-clean start
    assert "## Key files" not in out                   # reference section dropped
    assert "full handover in .magnolia/.handover-state.md" in out
    assert "final next step" in out                    # To do kept whole
    assert "second next step" in out
    assert "- line29: " + item in out                  # newest In progress items kept
    assert "- line0: " + item not in out               # oldest elided first
    original_items = {f"- line{i}: {item}" for i in range(30)}
    survivors = [l for l in out.splitlines() if l.startswith("- line")]
    assert all(l in original_items for l in survivors)  # contract: no mid-item cut


def test_budget_block_overflow_elides_oldest_items_first():
    # When items must be dropped, the oldest go first and survivors stay
    # whole — never a mid-line fragment of a dropped item. The big Key
    # files entry pushes the non-Done sections over the budget.
    old_item = "- oldthing " + "a" * 40
    new_item = "- newthing " + "a" * 40
    block = (
        f"## In progress\n{old_item}\n{new_item}\n\n"
        "## To do\n- next\n\n"
        "## Key files\n- " + "p" * 400 + "\n"
    )
    out = hv.budget_handover_block(block, 180)
    assert new_item in out
    assert old_item not in out
    original_items = {old_item, new_item, "- next"}
    survivors = [l for l in out.splitlines() if l.startswith("- ")]
    assert all(l in original_items for l in survivors)


def test_budget_block_overflow_tiny_budget_still_boundary_clean():
    # Even a budget far below any item's size must not produce mid-line
    # fragments — headers plus the pointer line, nothing else.
    block = (
        "## In progress\n- alpha " + "z" * 100 + "\n\n"
        "## To do\n- gamma " + "z" * 100 + "\n"
    )
    out = hv.budget_handover_block(block, 150)
    assert out.startswith("## ")
    assert "full handover in .magnolia/.handover-state.md" in out
    assert "z" * 50 not in out                         # no fragment of any item


def test_merge_prompt_has_size_and_stale_expiry_rules():
    """Contract: the merge LLM is told to compress old Done items, to cap
    their count, and to expire twice-stale items to tombstones
    (anti-windup, 2026-08-28; Done cap 2026-09-16)."""
    assert "ONE line" in hv.HANDOVER_MERGE_PROMPT
    assert "STALE EXPIRY" in hv.HANDOVER_MERGE_PROMPT
    assert "at most the 10 most recent" in hv.HANDOVER_MERGE_PROMPT


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
    cur = json.loads((store / hv.HANDOVER_CURSORS_DIR / "ses_a.json").read_text())
    assert cur["cursor"] == "m2"


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


# ---- generate_handover: per-session cursor model (multi-session) -------------

_NEW_SEP = "\n\n=== NEW SESSION TRANSCRIPT (since last handover) ===\n"
_BASE_HDR = "=== CURRENT HANDOVER (base to update) ===\n"
_CURSORS_DIR = ".handover-cursors"


def _merge_echo(system, user, max_tokens=2000, **kw):
    """Fake merge LLM: accumulate the new transcript onto the running base.
    generate_handover feeds the growing base back in on each session, so the
    final state accumulates every merged session's text. Output is prefixed
    '## ' to satisfy the handover-output validation contract."""
    base_part, new_part = user.split(_NEW_SEP, 1)
    base_part = base_part[len(_BASE_HDR):].strip()
    if base_part.startswith("(none yet"):
        base_part = ""
    parts = [base_part] if base_part else []
    parts.append(new_part.strip())
    return "## Done\n" + "\n".join(parts)


def _exporter(exports):
    return lambda sid: exports.get(sid)


def _session_cursor(store, sid):
    return json.loads((store / _CURSORS_DIR / f"{sid}.json").read_text())["cursor"]


def test_generate_multi_session_both_merged(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    exports = {
        "ses_a": {"info": {"id": "ses_a"}, "messages": [_msg("a1", "user", "dock KILDQ")]},
        "ses_b": {"info": {"id": "ses_b"}, "messages": [_msg("b1", "user", "run MD on WNPFF")]},
    }
    path = hv.generate_handover(str(store.parent), exporter=_exporter(exports), llm=_merge_echo)
    assert path is not None
    state = (store / hv.HANDOVER_STATE_FILE).read_text()
    assert "dock KILDQ" in state and "run MD on WNPFF" in state
    assert _session_cursor(store, "ses_a") == "a1"      # each cursor advanced
    assert _session_cursor(store, "ses_b") == "b1"      # independently


def test_generate_regression_just_completed_session_not_skipped(store):
    """The July-8/13 bug: the current near-empty session is already appended to
    the mapping at boot; old _latest_sid picks it and skips the just-completed
    session. That session's content MUST still be merged."""
    _write_mapping(store, ["ses_old", "ses_new"])
    exports = {
        "ses_old": {"info": {"id": "ses_old"},
                    "messages": [_msg("o1", "user", "fixed fle_end_2 sacct parsing")]},
        "ses_new": {"info": {"id": "ses_new"},
                    "messages": [_msg("n1", "user", "boot")]},   # near-empty live session
    }
    path = hv.generate_handover(str(store.parent), exporter=_exporter(exports), llm=_merge_echo)
    assert path is not None
    state = (store / hv.HANDOVER_STATE_FILE).read_text()
    assert "fixed fle_end_2 sacct parsing" in state     # <-- old code skips this


def test_generate_cursor_persists_across_boots(store):
    _write_mapping(store, ["ses_a"])
    exports = {"ses_a": {"info": {"id": "ses_a"}, "messages": [_msg("a1", "user", "dock KILDQ")]}}
    hv.generate_handover(str(store.parent), exporter=_exporter(exports), llm=_merge_echo)

    # second boot: ses_a unchanged, ses_b newly appended
    _write_mapping(store, ["ses_a", "ses_b"])
    exports["ses_b"] = {"info": {"id": "ses_b"}, "messages": [_msg("b1", "user", "run MD on WNPFF")]}
    calls = []

    def counting_llm(system, user, **kw):
        calls.append(user)
        return _merge_echo(system, user, **kw)

    hv.generate_handover(str(store.parent), exporter=_exporter(exports), llm=counting_llm)

    assert len(calls) == 1                                       # ses_a had nothing new
    assert "run MD on WNPFF" in calls[0].split(_NEW_SEP, 1)[1]   # ses_b fed as new
    state = (store / hv.HANDOVER_STATE_FILE).read_text()
    assert "dock KILDQ" in state and "run MD on WNPFF" in state  # base carried + new merged
    assert _session_cursor(store, "ses_a") == "a1"               # both cursors tracked
    assert _session_cursor(store, "ses_b") == "b1"               # independently, across boots


def test_generate_migrates_legacy_cursor(store):
    _write_mapping(store, ["ses_a"])
    # legacy single global cursor from the pre-fix model
    (store / hv.HANDOVER_CURSOR_FILE).write_text(json.dumps({"sid": "ses_a", "cursor": "a1"}))
    # no messages past a1 → nothing to merge → migrated cursor value is preserved
    exports = {"ses_a": {"info": {"id": "ses_a"}, "messages": [_msg("a1", "user", "old")]}}
    hv.generate_handover(str(store.parent), exporter=_exporter(exports), llm=_merge_echo)

    assert not (store / hv.HANDOVER_CURSOR_FILE).exists()        # legacy file removed
    assert _session_cursor(store, "ses_a") == "a1"              # folded into per-session model


def test_generate_llm_failure_midloop_keeps_prior_session(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    exports = {
        "ses_a": {"info": {"id": "ses_a"}, "messages": [_msg("a1", "user", "dock KILDQ")]},
        "ses_b": {"info": {"id": "ses_b"}, "messages": [_msg("b1", "user", "run MD on WNPFF")]},
    }

    def flaky_llm(system, user, **kw):
        if "run MD on WNPFF" in user:
            return None                     # fail on ses_b
        return _merge_echo(system, user, **kw)

    hv.generate_handover(str(store.parent), exporter=_exporter(exports), llm=flaky_llm)

    state = (store / hv.HANDOVER_STATE_FILE).read_text()
    assert "dock KILDQ" in state and "run MD on WNPFF" not in state
    assert _session_cursor(store, "ses_a") == "a1"                     # ses_a advanced
    assert not (store / _CURSORS_DIR / "ses_b.json").exists()          # ses_b NOT advanced


def test_generate_rejects_roleplay_output_then_succeeds_on_retry(store):
    """deepseek-v4-flash sometimes role-plays the transcript (no '## ' header).
    The merge must be rejected, retried once, and accepted when the retry is
    a valid handover."""
    _write_mapping(store, ["ses_a"])
    exports = {"ses_a": {"info": {"id": "ses_a"}, "messages": [_msg("a1", "user", "dock KILDQ")]}}
    calls = []

    def garbage_then_valid(system, user, **kw):
        calls.append(user)
        if len(calls) == 1:
            return "USER: and then I ran the docking again..."   # role-play corruption
        return "## Done\nmerged"

    path = hv.generate_handover(str(store.parent), exporter=_exporter(exports), llm=garbage_then_valid)
    assert path is not None
    assert len(calls) == 2                                         # exactly one retry
    assert "## Done" in (store / hv.HANDOVER_STATE_FILE).read_text()
    assert _session_cursor(store, "ses_a") == "a1"


def test_generate_persistent_corruption_writes_nothing(store):
    """If every attempt returns non-handover output, nothing enters the rolling
    state and no cursor advances — the corruption cannot leak in."""
    _write_mapping(store, ["ses_a"])
    exports = {"ses_a": {"info": {"id": "ses_a"}, "messages": [_msg("a1", "user", "dock KILDQ")]}}
    path = hv.generate_handover(
        str(store.parent), exporter=_exporter(exports),
        llm=lambda s, u, **k: "TOOL_CALL: gnina_dock(...)",
    )
    assert path is None
    assert not (store / hv.HANDOVER_STATE_FILE).exists()
    assert not (store / _CURSORS_DIR / "ses_a.json").exists()


def test_generate_truncated_merge_keeps_state_and_cursor(store):
    """finish_reason == 'length' means the merge output was cut at max_tokens;
    a whole-state rewrite would silently lose its tail, so the merge must be
    rejected and the cursor left un-advanced for a retry (2026-09-11)."""
    _write_mapping(store, ["ses_a"])
    (store / hv.HANDOVER_STATE_FILE).write_text("## Done\n- prior\n")
    export = {"info": {"id": "ses_a"}, "messages": [_msg("m1", "user", "new work")]}

    def truncated_llm(system, user, **kw):
        return "## Done\n- partial item cut mid", "length"

    assert hv.generate_handover(
        str(store.parent), exporter=lambda s: export, llm=truncated_llm) is None
    assert (store / hv.HANDOVER_STATE_FILE).read_text() == "## Done\n- prior\n"
    assert not (store / _CURSORS_DIR / "ses_a.json").exists()


def test_generate_finish_reason_stop_merges_normally(store):
    _write_mapping(store, ["ses_a"])
    export = {"info": {"id": "ses_a"}, "messages": [_msg("m1", "user", "dock KILDQ")]}

    def ok_llm(system, user, **kw):
        return "## Done\n- docked KILDQ\n", "stop"

    path = hv.generate_handover(str(store.parent), exporter=lambda s: export, llm=ok_llm)
    assert path is not None
    assert "docked KILDQ" in (store / hv.HANDOVER_STATE_FILE).read_text()
    assert _session_cursor(store, "ses_a") == "m1"
