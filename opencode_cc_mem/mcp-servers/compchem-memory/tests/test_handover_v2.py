"""Schema-v2 handover contract (2026-10-08; docs/boot-context-handover-v2-plan.md).

Covers: the A/B negative control on the real 2026-10-08 state-file fixture
(v1 drops ALL Done items on overflow; v2 must keep the newest), the fixed
boot-context composition (header → Done reserve → In progress → To do → index
line), the post-merge validator (caps, touched tags, date-due scan, ledger),
merge-time hold-back eligibility (dormancy-safe by construction), the
boot-only project-tier compaction flag, and the reconcile mechanism (Done ↔
not-started contradiction: prompt rule + deterministic flag-only scan, with
the real incident pair as an honest negative control)."""

import json
from pathlib import Path

import pytest

from compchem_memory import handover as hv
from compchem_memory.context_assembly import assemble_context
from compchem_memory.storage import ensure_project_store

FIXTURES = Path(__file__).parent / "fixtures"
REAL_STATE = (FIXTURES / "handover_state_20261008.md").read_text()
WINDOW_CHARS = 2000 * 4 - len("[SESSION HANDOVER]\n")   # the live session-tier window

_HEADER = ("Generated 2026-10-08T09:00:00+00:00 · last sessions: "
           "ses_x (2026-10-07, merged the branch split) · merged: 1")


def _state(header: str | None = None) -> str:
    parts = []
    if header:
        parts.append(header)
    parts.append(
        "## Done\n- newest fact: the branch split landed (7f6c70d)\n"
        "- middle fact: daemon registry lanes built\n"
        "- old fact: launcher profiles added\n\n"
        "## In progress\n- C1 soak day 4\n\n"
        "## To do\n- mid-soak checkpoint\n\n"
        "## Parked / Held\n- RC note (PARKED pending Tao's approval) (touched 2026-10-01)\n\n"
        "## Stale?\n- opencode-go verification (no activity)\n\n"
        "## Key files\n- runs/2026-06-15_kferq/\n"
    )
    return "\n\n".join(parts)


# ---- A/B negative control on the real fixture --------------------------------


def test_ab_real_fixture_v1_drops_all_done_v2_keeps_newest():
    """The 2026-10-08 failure, as a permanent regression gate: under v1 the
    overflow branch dropped every Done item; under v2 the newest Done items
    survive the same window."""
    v1 = hv.budget_handover_block_v1(REAL_STATE, WINDOW_CHARS)
    assert "ASTRA adaptor" not in v1          # the documented bad behavior

    v2 = hv.budget_handover_block(REAL_STATE, WINDOW_CHARS)
    assert "ASTRA adaptor" in v2              # newest Done survives
    assert "Branch split done" in v2          # second-newest too
    assert "held:" in v2                      # index line present
    assert ".handover-state.md" in v2         # pointer always present


def test_v2_real_fixture_excludes_reference_sections():
    out = hv.budget_handover_block(REAL_STATE, WINDOW_CHARS)
    assert "## Key files" not in out          # never injected under v2
    assert "softwares/bin/magnolia-agents-daemon" not in out
    assert "## Won't-do" not in out


# ---- composition contract -----------------------------------------------------


def test_composition_order_header_done_active_index():
    out = hv.budget_handover_block(_state(_HEADER), 100_000)
    lines = out.splitlines()
    assert lines[0].startswith("Generated ")
    assert out.index("## Done") < out.index("## In progress") < out.index("## To do")
    assert out.strip().endswith(".handover-state.md)*")


def test_parked_and_stale_never_injected_but_counted():
    out = hv.budget_handover_block(_state(_HEADER), 100_000)
    assert "RC note" not in out                          # parked content absent
    assert "PARKED pending" not in out
    assert "opencode-go verification" not in out         # stale content absent
    assert "held: 0 done · 1 parked · 1 stale" in out    # but counted


def test_done_reserve_keeps_newest_even_when_item_overflows_reserve():
    big = "- newest fact: " + "x" * (hv.RESERVE_DONE_CHARS + 100) + "\n"
    block = ("## Done\n" + big + "- old fact\n\n"
             "## In progress\n- wip\n\n## To do\n- next\n")
    out = hv.budget_handover_block(block, 5_000)
    assert "- newest fact:" in out               # at least the newest survives
    assert "- old fact" not in out               # reserve not extended


def test_active_elision_is_whole_item_oldest_last_section_priority():
    items = "\n".join(f"- wip{i}: " + "y" * 60 for i in range(10))
    block = ("## Done\n- done thing\n\n"
             "## In progress\n" + items + "\n\n"
             "## To do\n- next step\n")
    out = hv.budget_handover_block(block, 600)
    assert "- wip9:" in out                       # newest kept
    assert "- wip0:" not in out                   # oldest elided
    survivors = [l for l in out.splitlines() if l.startswith("- wip")]
    assert all(l in items for l in survivors)     # never a mid-line fragment
    assert "- next step" in out                   # To do still served


def test_pathological_budget_header_and_index_survive():
    out = hv.budget_handover_block(_state(_HEADER), 120)
    assert "Generated 2026-10-08T09:00:00" in out
    assert "held:" in out
    assert ".handover-state.md" in out
    assert "z" * 50 not in out                    # never a fragment (no z items, sanity)


def test_no_marker_legacy_state_is_graceful():
    legacy = "## Done\n- old work\n\n## In progress\n- wip\n\n## To do\n- next\n"
    out = hv.budget_handover_block(legacy, 100_000)
    assert "- old work" in out and "- wip" in out and "- next" in out
    assert "held: 0 done · 0 parked · 0 stale" in out   # no empty-section artifacts


def test_oversized_header_is_clamped_not_midsentence():
    header = "Generated x · " + "word " * 200
    out = hv.budget_handover_block(_state(header), 100_000)
    first = out.splitlines()[0]
    assert len(first) <= hv._HANDOVER_HEADER_MAX_CHARS + 2
    assert first.endswith("…")


# ---- post-merge validator -----------------------------------------------------


@pytest.fixture
def proj(tmp_path):
    ensure_project_store(str(tmp_path))
    return tmp_path


def test_validator_caps_trim_oldest_and_notice(proj):
    items = "\n".join(f"- done{i} (touched 2026-10-0{1 + i % 9})" for i in range(15))
    candidate = "## Done\n" + items + "\n"
    out = hv._post_merge_validate(candidate, "", proj / ".magnolia", str(proj))
    body = out.split("## Done\n", 1)[1]
    n_items = len([l for l in body.splitlines() if l.startswith("- ")])
    assert n_items == hv.DONE_CAP
    assert "done14" in out and "- done0 " not in out     # newest-first kept
    notices = (proj / ".magnolia" / ".distill-notices").read_text()
    assert "over cap" in notices


def test_validator_new_item_gets_today_unchanged_keeps_tag(proj):
    prev = "## Done\n- carried work (touched 2026-10-01)\n\n## To do\n- next\n"
    candidate = "## Done\n- carried work\n- brand new result\n\n## To do\n- next\n"
    out1 = hv._post_merge_validate(candidate, prev, proj / ".magnolia", str(proj))
    assert "(touched 2026-10-01)" in out1                # restored from prev
    assert f"(touched {hv._today()})" in out1            # new item stamped today
    # second merge carrying the same item unchanged must INCREMENT the counter
    out2 = hv._post_merge_validate(out1, out1, proj / ".magnolia", str(proj))
    ledger = json.loads((proj / ".magnolia" / hv.HANDOVER_STALENESS_FILE).read_text())
    merges = sorted(v["unchanged_merges"] for v in ledger.values()
                    if isinstance(v, dict))
    assert merges and max(merges) == 2                   # carried item counted across both merges


def test_validator_strips_llm_emitted_header(proj):
    candidate = "Generated something the LLM should not write\n\n## Done\n- work\n"
    out = hv._post_merge_validate(candidate, "", proj / ".magnolia", str(proj))
    assert not out.startswith("Generated")


def test_validator_past_due_date_notice(proj):
    candidate = "## To do\n- mid-soak checkpoint ~2026-01-01\n"
    hv._post_merge_validate(candidate, "", proj / ".magnolia", str(proj))
    notices = (proj / ".magnolia" / ".distill-notices").read_text()
    assert "past due" in notices


def test_validator_future_or_undated_no_notice(proj):
    candidate = "## To do\n- checkpoint ~2099-01-01\n- range 10-05 to 10-12\n"
    hv._post_merge_validate(candidate, "", proj / ".magnolia", str(proj))
    q = proj / ".magnolia" / ".distill-notices"
    assert not q.exists() or "past due" not in q.read_text()


# ---- hold-back eligibility (dormancy-safe by construction) --------------------


def _ledger_with(key_text, merges, touched):
    return {_item_key_wrap(key_text): {"unchanged_merges": merges,
                                       "touched": touched,
                                       "excerpt": key_text[:50]}}


def _item_key_wrap(text):
    return hv._item_key(text)


def test_hold_back_requires_both_merges_and_sessions():
    item = "## To do\n- quiet item (touched 2026-09-01)\n"
    ledger = _ledger_with("- quiet item", 3, "2026-09-01")
    dates = [f"2026-10-0{d}" for d in range(1, 6)]        # 5 sessions after touch
    assert hv._hold_back_eligible(item, ledger, dates)    # eligible: 3 merges + 5 sessions
    short_dates = dates[:2]
    assert not hv._hold_back_eligible(item, ledger, short_dates)   # M not met
    ledger2 = _ledger_with("- quiet item", 2, "2026-09-01")
    assert not hv._hold_back_eligible(item, ledger2, dates)        # N not met


def test_hold_back_dormant_project_never_ages():
    """The dormancy guarantee: zero merges since the touched date (a project
    asleep for a year) → nothing becomes eligible, wall-clock is irrelevant."""
    item = "## To do\n- sleeping item (touched 2025-01-01)\n"
    ledger = _ledger_with("- sleeping item", 50, "2025-01-01")
    assert not hv._hold_back_eligible(item, ledger, [])   # no sessions → no aging


def test_mapping_session_dates_dedup_and_sort(tmp_path):
    p = tmp_path / "opencode-sessions.jsonl"
    rows = [{"ts": "2026-10-08T09:00:00Z", "opencode_session_id": "a"},
            {"ts": "2026-10-08T10:00:00Z", "opencode_session_id": "b"},
            {"ts": "2026-10-01T08:00:00Z", "opencode_session_id": "c"},
            {"ts": "bad", "opencode_session_id": "d"}]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert hv._mapping_session_dates(p) == ["2026-10-01", "2026-10-08"]


# ---- reconcile scan (Done <-> not-started contradiction, flag-only) ------------


def test_reconcile_scan_flags_not_started_vs_done():
    state = (
        "## Done\n- Semantic recall scorer built (b2cd45e) + report wiring; "
        "arm3 measured. (touched 2026-10-08)\n\n"
        "## In progress\n- hsc70 bake-off — semantic scorer not started, "
        "wiring pending. (touched 2026-10-01)\n"
    )
    flags = hv.scan_reconcile_flags(state)
    assert len(flags) == 1
    w_item, _d_item, shared = flags[0]
    assert "not started" in w_item
    assert {"semantic", "scorer"} <= shared


def test_reconcile_scan_semantic_gap_is_honest_negative_control():
    """The REAL 2026-10-08 incident pair: 'P3 campaign rail — rest not started'
    vs 'Daemon build (3913ddc)'. Zero shared salient tokens, so the
    deterministic scan deliberately does NOT flag it — the merge-prompt
    RECONCILE rule is the catch for semantic contradiction. If the scan ever
    grows smart enough to catch this, flip the assertion."""
    state = (
        "## Done\n- **Daemon build (`3913ddc` on exp).** Registry lanes, "
        "`wait_for: job:<run_id>`, scheduled sweeps, persistent lane sessions; "
        "21 tests pass. (touched 2026-10-08)\n\n"
        "## In progress\n- **P3 campaign rail** — telemetry opener merged; "
        "rest not started. (touched 2026-10-07)\n"
    )
    assert hv.scan_reconcile_flags(state) == []


def test_reconcile_scan_open_residue_is_not_contradiction():
    """A genuinely-open follow-up on a built feature must NOT be flagged:
    'residue open' is not a negative-completion claim."""
    state = (
        "## Done\n- **Daemon build (`3913ddc`).** Registry lanes, wait_for, "
        "scheduled sweeps. (touched 2026-10-08)\n\n"
        "## In progress\n- **Agents daemon follow-ups.** COMPCHEM_TOOLS_PORT "
        "inheritance residue open. (touched 2026-10-08)\n"
    )
    assert hv.scan_reconcile_flags(state) == []


def test_reconcile_block_lists_pairs_and_clears_when_resolved():
    contradicting = (
        "## Done\n- semantic scorer built and wired into the report "
        "(touched 2026-10-08)\n\n"
        "## To do\n- build the semantic scorer — not started (touched 2026-10-01)\n"
    )
    block = hv._reconcile_block(contradicting)
    assert block.startswith("\n\n=== RECONCILE FLAGS")
    assert "semantic" in block and "scorer" in block
    assert "Done:" in block                       # pairs the two sides
    resolved = contradicting.replace(
        "build the semantic scorer — not started (touched 2026-10-01)",
        "re-register the semantic scorer as official metric (touched 2026-10-08)")
    assert hv._reconcile_block(resolved) == ""
    assert hv._reconcile_block("## Done\n- unrelated\n") == ""


def test_validator_reconcile_notice_fires_only_on_unresolved_candidate(proj):
    # candidate still carries the contradiction -> notice
    bad = ("## Done\n- semantic scorer built (touched 2026-10-08)\n\n"
           "## In progress\n- semantic scorer not started (touched 2026-10-01)\n")
    hv._post_merge_validate(bad, "", proj / ".magnolia", str(proj))
    notices = (proj / ".magnolia" / ".distill-notices").read_text()
    assert "contradiction" in notices
    # a candidate where the merge LLM resolved it -> no notice
    (proj / ".magnolia" / ".distill-notices").unlink()
    good = ("## Done\n- semantic scorer built (touched 2026-10-08)\n\n"
            "## In progress\n- re-register scorer as official metric "
            "(touched 2026-10-08)\n")
    hv._post_merge_validate(good, "", proj / ".magnolia", str(proj))
    q = proj / ".magnolia" / ".distill-notices"
    assert not q.exists() or "contradiction" not in q.read_text()


def test_generate_injects_reconcile_block_then_clears(tmp_path):
    ensure_project_store(str(tmp_path))
    store = tmp_path / ".magnolia"
    (store / "opencode-sessions.jsonl").write_text(
        json.dumps({"opencode_session_id": "ses_a", "ts": "1"}) + "\n")
    (store / hv.HANDOVER_STATE_FILE).write_text(
        "## Done\n- semantic scorer built (touched 2026-10-08)\n\n"
        "## In progress\n- semantic scorer not started (touched 2026-10-01)\n")
    export = {"info": {"id": "ses_a"},
              "messages": [{"info": {"id": "m1", "role": "user"},
                            "parts": [{"type": "text", "text": "status check"}]}]}
    captured = {}

    def fake_llm(system, user, max_tokens=2000, **kw):
        captured["user"] = user
        # the LLM obeys: resolves the contradiction this merge
        return ("## Done\n- semantic scorer built (touched 2026-10-08)\n\n"
                "## In progress\n- re-register scorer as official metric "
                "(touched 2026-10-08)\n")

    hv.generate_handover(str(tmp_path), exporter=lambda s: export, llm=fake_llm)
    assert "RECONCILE FLAGS" in captured["user"]        # flagged pair injected

    captured2 = {}

    def fake_llm2(system, user, max_tokens=2000, **kw):
        captured2["user"] = user
        return "## Done\n- more work (touched 2026-10-08)\n"

    export2 = {"info": {"id": "ses_a"},
               "messages": [{"info": {"id": "m2", "role": "user"},
                             "parts": [{"type": "text", "text": "again"}]}]}
    hv.generate_handover(str(tmp_path), exporter=lambda s: export2, llm=fake_llm2)
    assert "RECONCILE FLAGS" not in captured2["user"]   # resolved -> no block


# ---- session header ------------------------------------------------------------


def test_handover_header_format_and_cap():
    infos = [("ses_aaa", "2026-10-07", "merged the branch split"),
             ("ses_bbb", "2026-10-06", "soak checkpoint")]
    line = hv._handover_header(infos, 2)
    assert line.startswith("Generated ")
    assert "ses_aaa (2026-10-07, merged the branch split)" in line
    assert "merged: 2" in line
    assert len(line) <= hv._HANDOVER_HEADER_MAX_CHARS


def test_topic_from_transcript():
    t = "# session\n\n### user\nthe soak checkpoint shows zero twin kills today\n"
    assert hv._topic_from_transcript(t) == \
        "the soak checkpoint shows zero twin kills today"


# ---- project-tier compaction (boot-only flag) ----------------------------------


def _entry(project: Path, name: str, title: str) -> None:
    d = project / ".magnolia" / "entries"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\ntitle: {title}\ntype: scientific_finding\nconfidence: 0.9\n"
        f"observation_count: 3\n---\n\nBody about boot handover plumbing.\n")


def test_compaction_flag_boot_only(tmp_path):
    ensure_project_store(str(tmp_path))
    _entry(tmp_path, "a.md", "Boot handover budget failure analysis")
    compact = assemble_context("project boot context", str(tmp_path),
                               token_budget=4000, compact_project_tier=True)
    full = assemble_context("project boot context", str(tmp_path),
                            token_budget=4000)
    c = compact.content
    f = full.content
    assert ".magnolia/entries/a.md" in c                  # index line with path
    assert "conf 0.9" in c and "obs 3" in c
    assert "Body about boot handover plumbing." not in c  # body not injected
    assert "Body about boot handover plumbing." in f      # default keeps bodies
