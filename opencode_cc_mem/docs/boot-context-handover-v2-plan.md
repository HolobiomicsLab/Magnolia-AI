# Boot-context handover v2 — implementation plan

Date: 2026-10-08. Status: **Phases 0–3 + Level-1 tests + deterministic Level-2
simulation DONE (suite 699 green; sandbox boot-context 9.4 KB, handover block
6,667/7,981 chars, newest Done surviving). Remaining: real-LLM Level-2 merge,
Phase 4 migration prune (user-reviewed), Level-3 restart.**
Branch: `exp/distill-admission`. Audience: coding agent executing this plan.

## 1. Problem statement (verified 2026-10-08)

The handover exists to make session-to-session work seamless. It failed:

- `.handover-state.md` (12,816 chars, 13 Done / 22 In progress / 20 To do items)
  is the full truth. It is **never injected** into the agent.
- `boot-context.md` is the injected projection (`opencode.json:54`, `instructions`
  array). Its handover block gets the session tier: 20% of `token_budget=10000`
  = 2000 tokens ≈ 7,981 chars (`context_assembly.py:25,119`;
  `budget*4 - len("[SESSION HANDOVER]\n")`).
- Measured 2026-10-08: In progress (2,254 chars) + To do (2,943) = 5.2 KB — but
  the v1 priority set also includes `Stale?` + `Key files` (~2.9 KB), so the
  non-Done total ≈ 8.1 KB ≥ 7,981 → `budget_handover_block` (`handover.py:129`)
  takes the overflow branch: In progress + To do kept whole, **Done dropped
  entirely**, `Stale?`/`Key files` dropped, pointer line appended.
- Result: the newest facts (branch split `7f6c70d`/`da72cfd`/`fd9827f`, daemon
  build `3913ddc`) were invisible to the next session's agent. The agent had to
  reconstruct them from git + memory search.

Audit of the live state file (2026-10-08) also found:

- 3 expired items (consolidation queue now empty; promotion queue now empty;
  repo-root phantom store gone, `opencode_cc_mem/.magnolia` down to 6 files).
- Stale numbers: soak "day 3" (now day 4), labels 33→35, admission 58→73,
  backups 59→112.
- One item duplicated across two sections ("9 duplicate sets" in both
  `In progress` and `To do`).
- 13 Done items despite the merge prompt's "max 10" rule — prompt rules are
  soft; enforcement must be code.
- ~9 of 22 In-progress items carry parked/deferred/gated/not-started markers.

## 2. Design (agreed with user 2026-10-08)

Two-tier model stays: state file = full truth (git-tracked since `92d89ff`);
boot-context = budgeted projection. Changes:

1. **State file schema v2.** New section `## Parked / Held`. Markers that send
   an item there: `PARKED`, `deferred by user`, `gated on`, `not started`,
   `Optional:`. Every item carries a `(touched YYYY-MM-DD)` tag = date of last
   **content change**, never bumped by a carry-over merge.
2. **Session header** (user's design): first line of the handover block:
   `Generated <ISO datetime> · last sessions: <sid> (<date>, ≤8-word topic); …
   · merged this boot: n`. Sources: `opencode-sessions.jsonl`, handover cursors.
3. **Fixed boot-context composition:** session header → newest 2–3 Done items
   (reserved slice ~1,500 chars) → active In progress → active To do → index
   line: `*(D done · P parked · S stale items held — full handover in
   .magnolia/.handover-state.md)*`. `Parked / Held`, `Stale?`, `Key files`,
   `Won't-do` are **never injected**.
4. **Hold-back rule (no deletion, dormancy-safe).** Age is measured in
   **merge-time, not wall-clock**: an item becomes eligible to move to
   `Parked / Held` when unchanged across N=3 consecutive merges AND ≥ M=5
   sessions have passed since its `touched` date. A dormant project has zero
   merges → nothing ages → handover rides intact. Wall-clock applies only to
   date-bounded items (e.g. "checkpoint ~10-09"): past due → notice, not
   removal. Initial values N=3, M=5 are **parameters to tune from measurement**.
5. **Parking rights.** The merge may move items *into* `Parked / Held` when a
   marker is present. It must never unpark silently. Every move, trim, or
   demotion writes a notice row (receipts doctrine — nothing vanishes silently).
6. **Project-tier compaction (boot-context only).** Full `[PROJECT: …]` bodies
   (~23 KB for 5 entries today) become index lines:
   `- <title> (<type>, conf <c>, obs <n>) — .magnolia/entries/<file>`,
   top 8–12 by the existing rerank score. `memory_get_context` keeps full
   bodies — scope the compaction behind a flag so only `regenerate_boot_context`
   uses it.
7. **Budget rebalance.** Compaction frees ~5–6 KB of the 10,000-token budget.
   Rebalance `allocate_budget` so the session tier can use it (handover window
   ~8 KB → ~11–12 KB). **Measure first** (level-2 test), then set final numbers;
   do not raise `token_budget` total by default.

## 3. File touch list

| File | Change |
|---|---|
| `mcp-servers/compchem-memory/src/compchem_memory/handover.py` | `HANDOVER_MERGE_PROMPT` (schema v2, markers, header, touched tags, dedup); `budget_handover_block` v2 (fixed composition, Done reservation, index line); post-merge validator (caps, touched-tag backfill, notices); staleness ledger |
| `…/compchem_memory/context_assembly.py` | `assemble_context(..., compact_project_tier: bool = False)`; boot path passes `True`; budget rebalance after measurement |
| `…/compchem_memory/boot_context.py` | pass the flag; no other change (spec guardrail §1.4: still calls `assemble_context`) |
| notices queue writer (distill-notices) | new row kinds: `handover_demote`, `handover_trim`, `handover_date_due` |
| `tests/test_handover.py` + new `tests/test_handover_v2.py` | see §5 |
| `docs/` | this plan |

## 4. Execution phases with checkable items

### Phase 0 — baseline capture (read-only)

- [x] Copy the live `projects/xiulian/.magnolia/.handover-state.md` into
      `tests/fixtures/handover_state_20261008.md` (the known-overfull real file).
- [x] Copy this morning's real `boot-context.md` into
      `tests/fixtures/boot_context_before_20261008.md` (**known-bad artifact**:
      0 Done items survive). This is the negative-control baseline.
- [x] Baseline numbers recorded: state file 12,816 chars; boot-context 32,448
      chars; handover block in boot-context ≈ 5.4 KB with **0** Done items;
      boot-timing: handover 21,874 ms, total 56,207 ms (boot 2026-10-08 07:31 UTC).

### Phase 1 — merge-side schema v2 (`handover.py`) — DONE (commit pending)

- [x] `HANDOVER_MERGE_PROMPT`: `## Parked / Held` section + marker rules;
      `(touched YYYY-MM-DD)` on every item (unchanged items keep their tag);
      session-header line is machine-built (prompt forbids an LLM header —
      implementation choice: more robust than LLM-formatted topics, which the
      plan's "LLM supplies topic" left fragile); cross-section dedup rule;
      caps restated (Done ≤ 10, In progress ≤ 15, To do ≤ 20); Done order
      pinned NEWEST-FIRST (the live file is newest-first; v1's elision kept
      the wrong end — found and fixed during build).
- [x] Session-header builder (code): `_handover_header` — Generated ISO ·
      last ≤3 merged sids (date, ≤8-word topic via `_topic_from_transcript`)
      · merged count; prepended to the state file on every merge write.
- [x] Post-merge validator `_post_merge_validate` (code, deterministic):
  - [x] caps exceeded → trim oldest-first per section + `push_distill_notice`;
  - [x] unchanged item lost its `touched` tag → restored from previous state
        (normalized-text match) + notice;
  - [x] `Parked / Held` missing → accepted, stderr note (graceful);
  - [x] past due date → `handover_date_due`-style notice. Date detection is
        deliberately NARROW: only `~`-prefixed dates (commit hashes, ranges,
        parenthetical dates never trigger).
- [x] Staleness ledger `.handover-staleness.json`:
      per-item {touched, unchanged_merges, excerpt}; updated each merge;
      eligibility = unchanged ≥ 3 AND sessions-since ≥ 5.
- [x] Hold-back injection: eligible excerpts are appended to the merge input
      (`=== HOLD-BACK ELIGIBLE … ===`); moving is allowed, not required; never
      unpark silently (prompt rule).

### Phase 2 — budgeting v2 — DONE

- [x] Fixed composition order (§2.3); `RESERVE_DONE_CHARS = 1500`; the NEWEST
      Done item always survives even when it alone overflows the reserve.
- [x] `Parked / Held`, `Stale?`, `Key files`, `Won't-do` excluded; index line
      `*(N older items elided · held: D done · P parked · S stale — full
      handover in .magnolia/.handover-state.md)*` always appended.
- [x] Whole-item elision oldest-first within In progress/To do; never mid-line.
- [x] Header + index survive a pathological budget.
- [x] Old function kept as `budget_handover_block_v1` (delete after level 3).

### Phase 3 — project-tier compaction + rebalance — compaction DONE; rebalance NOT NEEDED at measured numbers

- [x] `assemble_context(compact_project_tier=False)` flag; boot path passes
      `True`; `memory_get_context` untouched (flag-scoped test).
- [x] `regenerate_boot_context` passes the flag.
- [x] Measured on the sandbox copy: handover block 6,667/7,981 chars with the
      current (pre-migration) state file — the window FITS; `allocate_budget`
      left unchanged (no rebalance warranted by data; revisit only if the
      post-migration fill or the Done-reserve size (4/13 shown) proves too
      tight in practice).

### Phase 4 — live migration

- [ ] Manual prune of the live state file (user-reviewed diff): remove the 3
      expired items; refresh numbers (soak day, labels 35, admission 73,
      backups 112); fix 2 wording drifts (semantic scorer built; MCP-pin race
      half-fixed by `a6a3956`); move the ~9 marked items to `Parked / Held`;
      add `touched` tags.
- [ ] This prune is the migration seed: the first merge under v2 starts from a
      clean schema.

### Phase 5 — verification (three levels, per entry `20260917_141822` doctrine:
unit tests alone do not catch this system's failure classes)

**Level 1 — unit tests (deterministic) — DONE, all green:**

- [x] Real fixture `handover_state_20261008.md`: v2 keeps the newest 2 Done
      items; **v1 on the same fixture keeps 0** (A/B test
      `test_ab_real_fixture_v1_drops_all_done_v2_keeps_newest` — the permanent
      negative control).
- [x] Pathological budget: header + index line survive; no mid-line cut.
- [x] No-marker state file: no empty-section artifacts; graceful output.
- [x] Dormant fixture: unchanged_merges irrelevant without new sessions —
      nothing held back (`test_hold_back_dormant_project_never_ages`).
- [x] Date-bounded item past due → notice written.
- [x] 15 Done items → trimmed to cap; notice written.
- [x] Cross-section dedup: contract pinned in the merge prompt
      (`DEDUP:` rule); code does not semantically dedup (documented).
- [x] Merge carrying an item unchanged preserves its `touched` date; two
      unchanged merges → counter 2.
- [x] Compaction: boot-context has index lines, not bodies;
      `memory_get_context` output unchanged (flag scoping).
- [x] Existing handover suites updated to the v1 name where they encode the
      v1 contract; full package suite **699 passed**.

**Level 2 — simulated boot (real data, sandbox; recipe proven 2026-09-23):**

- [x] Live xiulian `.magnolia` store copied to `/tmp/mag-sim` (keys unset →
      deterministic rerank): `regenerate_boot_context` ran the real pipeline.
      Results: boot-context 32,448 → 9,437 chars; handover block 6,667/7,981
      chars; "ASTRA adaptor" + "Branch split done" (the facts lost on
      2026-10-08) present; Key files absent; index line
      `*(held: 9 done · 0 parked · 1 stale …)*` (0 parked = legacy file has no
      Parked section yet — Phase 4 migration adds it).
- [ ] Repeat with **one real deepseek-flash merge** on the sandbox copy: verify
      the LLM actually produces `Parked / Held` + `touched` tags + the
      newest-first Done order. If it does not, harden the merge prompt
      **before** relying on budgeting. (Next session; needs a valid API key in
      the shell env — the bashrc key is known-stale.)
- [x] Measured: handover block 6,667 chars vs 7,981 window; Done survival 4/13
      (reserve-driven); index counts match the state file (13 done, 1 stale).

**Level 3 — real boot:**

- [ ] One actual opencode restart. Check `boot-timing.jsonl` steps; inspect the
      injected boot-context: header present, newest Done present, parked absent,
      index counts match the state file.
- [ ] Only then delete `budget_handover_block_v1` and close this plan.

**Negative controls (mandatory):**

- [ ] The 2026-10-08 fixture must keep failing under v1 and passing under v2.
- [ ] An over-budget fixture must degrade gracefully (whole-item elision +
      pointer + index), never mid-line.

## 5. Parameters to tune from measurement (not guessed)

| Parameter | Initial | Decide after |
|---|---|---|
| N (unchanged merges before hold-back) | 3 | 3–5 live boots |
| M (sessions since touched) | 5 | same |
| `RESERVE_DONE_CHARS` | 1500 | level-2 measurement |
| Session-tier floor after rebalance | TBD | level-2 measurement |
| Project-tier index size | 8–12 entries | one week of use |

## 6. Rollback and safety

- All changes behind the existing branch; no new env flags except none needed —
  rollback = `git revert` + restart. `budget_handover_block_v1` kept until
  level 3 passes.
- State file is git-tracked (`92d89ff`); migration prune is recoverable.
- Live store is never touched by level 1/2 tests (sandbox copies only).
- After level 3: record the outcome (`success_pattern` or `failure_pattern`)
  with the measured numbers; record the still-open `failure_pattern` for the
  2026-10-08 Done-elision finding regardless of outcome.

## 7. Out of scope (do not build here)

- Expiry/removal of items (hold-back only).
- Changes to `memory_get_context` project-tier bodies.
- Total `token_budget` raise (decision deferred to measurement).
- Retirement v2 activation (separate phase-gate item).
