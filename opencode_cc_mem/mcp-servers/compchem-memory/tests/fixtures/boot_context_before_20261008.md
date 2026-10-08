[PROJECT GOAL]
# Goal: xiulian

## One-line summary

修炼. dedicated to improve magnolia itself

## Background

Magnolia is a memory-backed agent system that relies on persistent state and retrieval to support long-running reasoning tasks. "Xiulian" (修炼) — meaning "cultivation" or "training" — reflects the aim of iteratively refining Magnolia's own architecture, memory management, and reasoning capabilities. This project seeks to identify and implement systematic improvements to the system’s core, making it more efficient, adaptive, and robust over time.

## Inputs

- Current Magnolia source code and architecture documentation
- Performance logs and profiling data (e.g., memory usage, reasoning latency, retrieval accuracy)
- Existing test suites and benchmarks for agent tasks
- User feedback or failure cases (if available) — **TODO: user to provide**
- A list of desired improvement areas (e.g., memory consolidation, context handling) — **TODO: user to specify**

## Approach

1. **Audit current performance**: Profile Magnolia’s memory access patterns, reasoning loops, and error rates to identify bottlenecks and failure modes.
2. **Prioritize improvements**: Based on audit, select 2–3 high-impact areas (e.g., cache eviction policy, long-term memory decay, query embedding) for first iteration.
3. **Design and implement changes**: Prototype modifications, possibly using A/B testing or sandboxed agents, ensuring backward compatibility.
4. **Validate with benchmarks**: Run existing tasks and new stress tests to measure improvements in speed, accuracy, and memory stability.
5. **Iterate**: Use the results to refine further; treat xiulian as a continuous cultivation cycle.

*Open issue: exact improvement list depends on user’s priorities — **TODO: clarify with user**.*

## Success criteria

- Measurable improvement in at least one key metric (e.g., 20% reduction in reasoning latency, 15% higher retrieval precision, 30% lower memory footprint) compared to baseline.
- No regression in existin

---

[SESSION HANDOVER]
## In progress
- **C1 soak — day 3 of 7 (10-05 → 10-12).** admission-log 58 decisions, action-retrieval 58 new-schema rows, queue empty, floor suppressed 3, labels 33, auto-band 2 receipts. Twin kills still 0 (guard live, no twins seen). Mark 10-06 as behavior boundary.
- **v2 burn-in** — needs a real launcher-launched TUI session doing substantial work (only user-level check left; all headless paths verified).
- **Volume tuning** — floor live; arm3 volume 43.6% vs ≤40% gate (3.6 pts short).
- **Stage-2 sibling-dedup** — clause built behind flag; live validation at soak close.
- **Retirement v2** — built dormant; activation + threshold sanity at phase gate (needs ≥3 days exposure).
- **hsc70 bake-off** — arm3 done; next is the semantic scorer as the official metric (TEST-PLAN re-registration) + human spot-check of the 10 recovered pairs.
- **Label audit** — deferred by user (2 judge-rejected workhorses are label double-counts; keep as honest history).
- **LLM judge certification sample** — accept-side recall unmeasured (needs snapshot-before-merge fix).
- **Corpus-level coverage check** — not run.
- **xiulian 9 pending duplicate sets (live tree)** — new, unhandled.
- **Communication promotion proposal** — pending (`promotion-proposal.json`; needs own project session).
- **`exp/distill-admission` remaining slices** — idle-gated extraction (R3 done), admission gate (R1/R2/R4 done), retirement v2 (built), auto-band + label store (done). Merge at phase gate.
- **gpx4 protocol test panel + first 2-arm bake-off** — not started, user deferred.
- **master → experimental cherry-pick policy — held items.** P0.4 (`1c4cb8f`, `c874c97`), P1/A0 (`15bb5ac`), P1/run_id (`e05fc9c`), P1/A1 (`e23dfb2`), P1/receipts (`81fd551`), P1/verifier+gates (`7af0da0`) exp-only.
- **D3 canary scheduler** — verified live; watching for recurrence.
- **B (tentative-tier redesign)** — gated on D2 data.
- **msc report-generation follow-ups** — PARKED pending lfnothias.
- **RC note (`v0.1.0-rc1`)** — PARKED pending Tao's approval.
- **P3 campaign rail** — telemetry opener merged; rest not started.
- **Agents daemon follow-ups.** `from-unknown` fixed via registry; per-project config rendering (MCP-pin race) still open.
- **ad_verum consolidation** — deferred by user.

## To do
- **First real idea ticket through the daemon loop** (watched once) — literature sweep → digest → ticket → xiulian lane → replay gate → reply.
- **Run `astra validate`** on a generated document with lfnothias' checker; confirm field-level conformance.
- **Ad Verum PR #2 review with lfnothias** (HIGH PRIORITY, `todo.md:41`) — the adaptor gives concrete material.
- **Decide `~/.bashrc` fix for bare `magnolia`** — PATH-preference line vs alias (`magnolia-exp` taken); user to pick.
- **Monitor C1 soak** — `r4_dup_batch`, same-batch twins; mid-soak checkpoint ~10-09.
- **Measure floor effectiveness** — queue weak-band share + suppressed log over next sweeps.
- **Spec details to pin:** boot-context inclusion counts as surfacing; auto-confirm routes through conflict holds; error_resolution/failure_pattern merge path; arm-1 fidelity tolerance under v2; v2 boot-context injection mechanism.
- **Decisions still open:** R8 auto-merge same-session twins (deferred until R4 proves out).
- **hsc70_new session to resolve conflicting entries** (backup safe): confirm `20260618_124742_555469`, correct/limit `20260625_100550_729733`; review proposals in `reflex/`; run `memory_health_check`.
- **Complete the literature paper check:** second letter to ingest arXiv:2609.27334 into the right `_bge` KB.
- **arXiv API etiquette section** in `projects/literature/inbox/README.md`.
- **hsc70_new revisit (15 min):** 1 promotion proposal, 8 dup sets, 45 confirmable entries.
- **gpx4 revisit (ad_verum):** 2 dup sets, 11 confirmable; wrong `--covalent_rec_res` flag correction.
- **Review 9 duplicate sets pending in xiulian (live tree)** + ~37 remaining obs≥3 confirmables.
- **Decide fate of TWO stray stores** — `opencode_cc_mem/.magnolia/` (73 files) and repo-root `.magnolia/` (2,848 files, phantom store). #25 rejection keeps them as separate entries; investigation open.
- **P1 follow-up slices:** retire mandatory AGENTS.md self-report; coverage heartbeat; A2 historical backfill (script done, execution at phase gate).
- **P2:** notification bridge + bounded auto-retry + job health monitor — on experimental (`59c705f`, `76cece1`); confirm status.
- **Publish the RC** once note approved: tag `v0.1.0-rc1` on master head, flip public, pre-release, anonymous-clone check. `origin` now configured (HolobiomicsLab/Magnolia-AI); branches pushed.
- **Confirm A1–A3 active on live system** (backups dir was 59 files).
- **Frontier assessment §5 remaining:** reposition pitch; harness-memory cohabitation rule; Bucket 2.
- **Investigate recap completion-state mechanism** (stale pending actions).
- **Optional:** contribution-licensing line; in-house retrieval eval on `_bge` KBs; worktree env README line (`uv run --with pytest python -m pytest`); handover skip-before-export follow-up (15 xiulian sessions blind-export); `purpose` field in `llm-timing.jsonl`; receipt hand-label spot-check; re-verify k3 judge; judge-saturation human-labeled set.

*(older items elided to fit the boot budget — full handover in .magnolia/.handover-state.md)*

---

[PROJECT: Handover 'restarted' context loss: boot-context tail-slice drops 'In progress'/'To do']
---
confidence: 0.85
created: '2026-09-16T16:56:41.554093+02:00'
description: 'SYMPTOMS: After a session restart, a one-word cue like ''restarted''
  failed to connect to the post-restart verification checklist; the agent had no checklist
  in context. ROOT CAUSE: The handover source '
id: '20260916_165641_554093'
observation_count: 5
observed_in_sessions:
- ses_f556ccd48ffew2HrfofZKXak8E
- ses_f519131bcffeiCT6rN2Fq7S71c
opencode_session_id: ses_f556ccd48ffew2HrfofZKXak8E
source: opencode_distill
tags:
- boot-context
- handover-state
- budget_handover_block
- memory_get_context
- restart
- context-assembly
- truncation
title: 'Handover ''restarted'' context loss: boot-context tail-slice drops ''In progress''/''To
  do'''
tools:
- memory_get_context
type: error_resolution
updated: '2026-09-17T08:55:24.184376+00:00'
---

SYMPTOMS: After a session restart, a one-word cue like 'restarted' failed to connect to the post-restart verification checklist; the agent had no checklist in context. ROOT CAUSE: The handover source `.handover-state.md` (~17,000 chars) is permanently ~3.5x over the boot-context handover budget (20% of a 6,000-token budget = ~4,800-character hard cap). `budget_handover_block` has two modes: (1) NORMAL — elides oldest 'Done' items, safe, and annotates '(older Done items elided — full history in .handover-state.md)'; (2) OVERFLOW — triggered when the file still doesn't fit with zero Done items, the code itself calls this branch 'pathological' and simply keeps the LAST ~4,800 characters. Because the handover is ordered Done / In progress / To do / Stale? / Key files, tail-slicing preserves the end (Key files) and discards 'In progress' and 'To do'. In this incident the cut landed mid-word inside the Key files list ('mem/mcp-servers/…'), removing the entire verification checklist before the agent saw it. FIX (proposed): make the OVERFLOW fallback drop low-priority sections (e.g. Key files / long path lists) instead of slicing mid-document, so 'In progress' and 'To do' always survive whole. CAVEAT: This is a boot-context budgeting bug, not the older 'stale handover' bug — the state file was rewritten by the handover step ~2 s before boot-context ran, and the two views ('restarted' failure vs. later `memory_get_context`) differed only because the rewritten handover was slightly shorter so the cut landed earlier and the tail of 'To do' survived. Memory retrieval cannot rescue this because the checklist was never written as a memory entry; it exists only inside the handover document.

## Observation 2 (2026-09-16)

In compchem-memory boot context generation, regenerate_boot_context -> assemble_context(token_budget) -> allocate_budget gives the session tier 20% of budget (1200 tokens at token_budget=6000), capping the handover block at ~4781 chars. When the handover blocks exceed this, budget_handover_block (handover.py, ~lines 118-121) falls into a pathological branch `return block[-char_budget:]` — a blunt tail slice that cuts mid-line/mid-word and can delete the entire '## To do' and '## In progress' sections. Observed effect: the To do list was silently absent from boot context after a restart. SYMPTOMS: new session boots with truncated/garbled handover lacking its actionable recap, despite a full .handover-state.md on disk. FIX (proposed): replace the tail slice with priority-ordered reduction — keep '## In progress' and '## To do' complete always; drop '## Key files' then '## Stale?' (each replaced with a one-line pointer to .magnolia/.handover-state.md); only if To do + In progress alone overflow, elide whole items oldest-first with a count note; every compressed handover ends with a pointer line to the full state file. CAVEAT: diagnosis is from reading handover.py/boot_context.py and matching the observed symptom; the fix was proposed but not yet implemented or measured.

## Observation 3 (2026-09-16)

SYMPTOMS: After a restart, the boot handover block was missing the session's entire '## To do' (and part of '## In progress') list; the block had been cut mid-line. CAUSE: budget_handover_block (handover.py) took the 'pathological' overflow path `return block[-char_budget:]` — a raw tail-slice that cuts anywhere, including mid-item and through the priority sections, and loses exactly the newest/most-actionable content. FIX: rewrote the overflow branch to keep '## In progress' and '## To do' items WHOLE (eliding whole items oldest-first), drop reference sections ('## Stale?', '## Key files') first, and always append a pointer '*(older items elided to fit the boot budget — full handover in .magnolia/.handover-state.md)*'. Added a shared module-level `_split_items(section_body)` helper (splits a '## Section' body into header + item chunks; a new '- '/'* ' line starts a chunk, continuation lines extend it) and refactored the Done-elision path to use it. NEVER cut mid-line. Also raised boot_context.regenerate_boot_context token_budget 6000→10000. VERIFY: replaced the old test that asserted the buggy tail-slice contract with tests asserting both actionable sections survive whole, oldest items elided first, tiny budgets stay boundary-clean, and no mid-item fragments in output; 427 tests pass.

## Observation 4 (2026-09-16)

budget_handover_block() in handover.py used a fallback return block[-char_budget:] when even the non-Done sections (In progress + To do + Stale? + Key files) exceeded the boot window. This tail-slice cut mid-line ANYWHERE and, on 2026-09-16, dropped a restarted session's entire In progress / To do list from boot context (handover state was ~17 KB against a ~4.8 KB window = 3.5x over). Root cause: a character-window slice with no notion of section/item boundaries. Fix: when non_done_total >= char_budget, keep '## In progress' and '## To do' items whole and drop the reference sections ('## Key files', '## Stale?'), eliding whole items oldest-first (keep newest), and always end with a pointer '*(older items elided to fit the boot budget — full handover in .magnolia/.handover-state.md)*'. Never cut mid-line. A shared _split_items helper (used by both the Done elision and the overflow path) chunks a section body into whole items: a new '- '/'* ' line starts a chunk, blank lines separate, continuation lines extend. Output may slightly exceed the budget only when the budget is smaller than the ~95-char pointer itself. CAVEAT: this is a boot-context safety net; it does not by itself shrink the state file — the merge-side Done cap and the raised budget do that. Evidence: handover.py overflow branch; prior behavior documented by staging entries 20260916_163619 (tail-slice root cause) and 20260916_165641 (restarted-context loss).

## Observation 5 (2026-09-17)

Symptoms: after a restart a session lost its whole actionable recap; seen 2026-09-16. Cause: when handover state exceeded the boot window with no Done items to elide (~17 KB state vs ~4.8 KB session-tier share), the OVERFLOW branch of budget_handover_block tail-sliced the block mid-word, cutting anywhere including straight through '## In progress' and '## To do'; the code itself called this branch 'pathological'. Fix (commit 92d89ff, 'fix(handover): keep In progress/To do whole on boot-budget overflow'): rewrite the overflow path to keep In progress and To do items whole (elide oldest first), drop the reference sections (Stale?, Key files), and always end with a pointer to .handover-state.md; extract a shared `_split_items()` helper reused by both the Done elision and the overflow path; raise regenerate_boot_context's token budget 6000 -> 10000. Also track `.magnolia/.handover-state.md` in the nested versioning repo so merge-aged-out items (Done cap, stale expiry) remain recoverable from git. Tests: test_handover.py, test_versioning.py. CAVEAT: verified by 433 passing tests and diff review; the '17 KB vs 4.8 KB' figures come from the 2026-09-16 observation, not a re-measured current state.


---

[PROJECT: Boot handover block is tail-sliced mid-line when priority sections exceed the 4.8 KB budget]
---
confidence: 0.92
created: '2026-09-16T16:36:19.996006+02:00'
description: Root-cause for why the agent failed to connect a user's 'restarted' message
  to the previous session's post-restart verification checklist (xiulian, 2026-09-16
  16:18 boot). VERIFIED mechanism, not hypo
id: '20260916_163619_996006'
observation_count: 2
observed_in_sessions:
- ses_f556ccd48ffew2HrfofZKXak8E
- ses_f51e210baffezNxHa1cPKjWUNM
opencode_session_id: ses_f556ccd48ffew2HrfofZKXak8E
source: opencode_distill
tags:
- handover
- boot-context
- budget_handover_block
- context_assembly
- tail-slice
- restart-context-loss
- xiulian
title: Boot handover block is tail-sliced mid-line when priority sections exceed the
  4.8 KB budget
tools:
- codegraph_context
- run_shell
- read
type: scientific_finding
updated: '2026-09-17T07:06:31.484285+00:00'
---

Root-cause for why the agent failed to connect a user's 'restarted' message to the previous session's post-restart verification checklist (xiulian, 2026-09-16 16:18 boot). VERIFIED mechanism, not hypothesis: (1) `boot_context` regenerates via `assemble_context(token_budget=6000)`; `allocate_budget` gives the session tier 20% = 1200 tokens; `_get_session_context` then budgets the handover block to `1200*4 - 17 = 4783 chars` (handover.py). (2) The rendered handover view (tombstones stripped) measured 16,914 chars on disk, and `.handover-state.md` itself is 17,508 chars (mtime 16:18:06, same boot). (3) `budget_handover_block`'s priority-aware path (section-aware Done-elision, from the 2026-08-28 fix) only helps when Done-elision alone brings the block under budget; when non-Done sections alone exceed the budget it takes the pathological branch `return block[-char_budget:]` (handover.py:118-121) — a blunt tail slice that can start mid-word and cuts through `## In progress` / `## To do`. Observed: the session's boot-context.md SESSION HANDOVER began mid-word at 'mem/mcp-servers/...' with only the Key files tail present; a later in-session `memory_get_context` returned a *different* mid-line tail ('in `~/repos/project_magnolia-exp`...') because by then the boot handover merge had rewritten the state file. Fix direction (proposed, NOT implemented): make the overflow fallback section-aware — never cut inside `_PRIORITY_SECTIONS`; drop `## Key files` / `## Stale?` first, keep `## To do` and `## In progress` whole, add a pointer to `.handover-state.md`; and/or raise the boot `token_budget`; and/or cap Done items at merge time so the state file stays bounded. CAVEAT: verified for the xiulian project on 2026-09-16; the char/4 token approximation inflates real token counts for paths/CJK, so the real block is ~4–5k tokens over the char budget in practice. Applies to every restart of any project whose handover state exceeds ~4.8 KB (i.e. permanently for active projects) until fixed.

## Observation 2 (2026-09-17)

First live data from boot-timing.jsonl (project xiulian, restart 2026-09-17 06:46-06:48 UTC) decomposed the compchem-memory server boot: server_import 948ms (first import), startup_scan 14.0-18.3s, handover 82.8s (+92.9s on the duplicate pass), boot_context ~1.8s, audit ~0ms; two overlapping passes totalled 98.7s and 113.0s. The handover step — LLM-merging backlogged session transcripts on first restart — accounts for ~85% of boot. This confirmed the 2026-08-28 prediction that the first boot after a restart LLM-merges backlogged sessions sequentially (extra boot latency, not a hang). Because the boot runs in a background daemon thread while the user types, perceived first-response latency still improved (user confirmed: 'It is indeed faster'). Determined by reading projects/xiulian/.magnolia/boot-timing.jsonl and llm-timing.jsonl. CAVEAT: only one restart observed; the 83-93s handover cost is specific to a backlog of unmerged sessions and should shrink markedly on subsequent restarts once cursors are advanced.


---

[PROJECT: Boot timing decomposition: this session booted in 117.4 s (startup_scan 59.5 s + handover 56.5 s)]
---
confidence: 0.9
created: '2026-09-24T08:29:03.343289+02:00'
description: 'Boot of the Magnolia session at 2026-09-24 06:08:40 UTC (08:10 local)
  totalled 117.4 s, decomposed from boot-timing.jsonl as: server_import 1.1 s, startup_scan
  59.5 s, handover 56.5 s, boot_context 1.'
id: '20260924_082903_343289'
observation_count: 2
observed_in_sessions:
- ses_f2df831f5ffetb3SnG8TDiGee9
- ses_f556ccd48ffew2HrfofZKXak8E
opencode_session_id: ses_f2df831f5ffetb3SnG8TDiGee9
source: opencode_distill
tags:
- boot-performance
- boot-timing
- boot-timing.jsonl
- compchem-memory
- handover
- instrumentation
- llm-timing
- llm-timing.jsonl
- magnolia-boot
- server.py
- startup_scan
- xiulian
title: 'Boot timing decomposition: this session booted in 117.4 s (startup_scan 59.5
  s + handover 56.5 s)'
tools:
- compchem-tools_run_shell
type: scientific_finding
updated: '2026-09-24T08:29:03.343289+02:00'
---

Boot of the Magnolia session at 2026-09-24 06:08:40 UTC (08:10 local) totalled 117.4 s, decomposed from boot-timing.jsonl as: server_import 1.1 s, startup_scan 59.5 s, handover 56.5 s, boot_context 1.4 s, audit 10 ms. The startup_scan window (06:08:41-06:09:39) contains ~25 small LLM calls of ~1.7 s each (from llm-timing.jsonl window 06:08:51-06:09:39); the handover window contains 2 large calls of 19.7 s and 13.9 s at 06:10:21 and 06:10:36 (~34 s of LLM plus merge overhead). .magnolia/sessions contains only 4 files (all July/early Sept) so the 25 startup_scan calls are NOT re-distillation of local Magnolia session JSONLs. Determined how: reading projects/xiulian/.magnolia/boot-timing.jsonl and llm-timing.jsonl via run_shell. CAVEAT: llm-timing.jsonl has no 'purpose' field (records only provider/model/ms/outcome/chars), so the identity of the 25 calls is inferred from timing windows, not logged — treat the composition as hypothesis. Applies to the live master tree; exp-branch boot behaviour not measured here.

## Corroborating observations (merged)

- (session ses_f556ccd48ffew2HrfofZKXak8E) Boot step timing instrumentation landed: JSONL boot-timing rows + log lines, 425 tests green

Implemented boot timers in server.py's startup worker: each step writes one JSON row to projects/xiulian/.magnolia/boot-timing.jsonl and echoes `[boot-timing] <step>: <ms>` to the server log. Rows: server_import (module load), startup_scan (session distillation sweep), handover (LLM merge of recent sessions into the state file — usually the slow step), boot_context (regeneration of boot-context.md), audit, total. Per-LLM-call detail remains in llm-timing.jsonl, so the two files together decompose a restart. 425 tests passed. This instrumentation is what makes it possible to observe whether capping Done (fix 3) and the budget raise (fix 2) reduce boot merge cost. CAVEAT: timings are per-restart wall-clock on this host; the two files must be read together to attribute cost correctly.


---

[PROJECT: Boot context budget raise: token_budget 6000 -> 10000 to give the handover an ~8 KB window]
---
confidence: 0.7
created: '2026-09-16T17:37:34.036387+02:00'
description: regenerate_boot_context currently calls assemble_context(token_budget=6000);
  the session tier receives 20% (1200 tokens, ~4781 chars), which starves the handover.
  Proposed change (one line in boot_con
id: '20260916_173734_036387'
observation_count: 5
observed_in_sessions:
- ses_f556ccd48ffew2HrfofZKXak8E
- ses_f51e210baffezNxHa1cPKjWUNM
opencode_session_id: ses_f556ccd48ffew2HrfofZKXak8E
source: opencode_distill
tags:
- compchem-memory
- boot_context.py
- token_budget
- allocate_budget
- handover-window
title: 'Boot context budget raise: token_budget 6000 -> 10000 to give the handover
  an ~8 KB window'
tools: []
type: parameter_guidance
updated: '2026-09-17T07:06:32.485318+00:00'
---

regenerate_boot_context currently calls assemble_context(token_budget=6000); the session tier receives 20% (1200 tokens, ~4781 chars), which starves the handover. Proposed change (one line in boot_context.py): token_budget 6000 -> 10000, growing the handover window from ~4.8 KB to ~8 KB. Side effect: the project tier (70%) grows by ~4k tokens of reranked entries at boot — judged harmless since boot context is small relative to the model window. Alternative knobs considered and rejected as more invasive: raising the session-tier share inside allocate_budget (shared with memory_get_context and spec-guardrailed '§1.4 must call assemble_context, not custom assembly'), or a boot-specific session-tier floor. CAVEAT: the 20%/70% split and ~4781-char cap were read from the code, not measured in a live boot; the side effect on retrieval was reasoned, not benchmarked.

## Observation 2 (2026-09-16)

Values chosen (2026-09-16) and what they replace:

1. `boot_context.regenerate_boot_context(token_budget)`: 6000 → 10000. Why: the session tier gets 20% of the total (`allocate_budget`), so the handover block was capped at ~4,783 chars while the rendered handover state was 16,914 chars (xiulian, 2026-09-16 boot) — every restart took the overflow branch. At 10,000 the handover window is ~8 KB.

2. `HANDOVER_MERGE_PROMPT` SIZE rule: added "keep at most the 10 most recent Done items" (no count cap before; the one-line-compression rule existed but the LLM often kept long items, and the state grew unboundedly — 17 KB). The stale-expiry machinery (Stale? → tombstone after two inactive merges) already existed and was kept as-is.

3. `budget_handover_block` overflow branch: replaced `block[-char_budget:]` tail-slice with priority-aware reduction (In progress/To do items whole, newest kept oldest elided; Stale?/Key files dropped; pointer line to .handover-state.md). This is a behavior contract, not a tunable.

4. `versioning._GITIGNORE`: added `!/.handover-state.md` so merge-time aging is recoverable from the nested git repo.

Why now: the tail-slice silently dropped a restarted session's entire To do list (see failure_pattern staging entries 20260916_163619 / 20260916_165641). Fixes 1+3+4 stop the bleeding; the judgement-based pruning (self-reflex forget pass) is deliberately deferred to its own design (repo todo.md).

CAVEAT: budget math uses the len/4 char heuristic, so 8 KB ≈ 2,000 tokens is approximate (paths/CJK inflate it). If the handover state grows past ~8 KB again, the overflow branch now degrades gracefully instead of slicing mid-line, but raising the budget further should be weighed against boot-context size.

## Observation 3 (2026-09-16)

Value changed: boot_context.regenerate_boot_context(token_budget) 6000 → 10000; and HANDOVER_MERGE_PROMPT now caps '## Done' at the 10 most recent items. Why: the session tier of the boot budget (20% share) at 6000 tokens gave the handover block only ~4.8 KB against a ~17 KB .handover-state.md, so the handover was being starved/tail-sliced. 10000 tokens → ~8 KB window, enough for In progress + To do + Stale? whole with only Key files/Done dropped. .handover-state.md was also added to versioning tracked paths (nested git repo alongside entries/ and staging/) so merge-time aging is recoverable. Applies at the next restart when the boot scan regenerates boot-context.md and the merge applies the Done cap.

## Observation 4 (2026-09-16)

Three parameter changes landed 2026-09-16 for the handover/boot pipeline. (1) boot_context.regenerate_boot_context token_budget: 6000 -> 10000 (value replaced: 6000; value chosen: 10000) — at 6000 the session tier's 20% share was only ~4.8 KB against a ~17 KB handover state, so the handover was starved and tail-sliced mid-line; at 10000 the 20% session tier yields ~8 KB. (2) HANDOVER_MERGE_PROMPT: added a hard Done cap — 'keep at most the 10 most recent Done items' — because the pre-existing SIZE rule ('compress every other Done item to ONE line, under ~150 lines') was not being followed by the merge LLM (a single Done bullet had grown to ~1.5 KB). (3) versioning.py: added '!/.handover-state.md' to _GITIGNORE so the handover state file is tracked in the entries/staging git repo (joins '!/entries/' and '!/staging/'); existing repos repair the .gitignore on next ensure_repo call. NOTE: prompt text wraps across lines, so tests must assert a substring that does not span a newline (e.g. 'at most the 10 most recent', not the full sentence). CAVEAT: the boot budget is a token count while the handover window is a char budget (session tier = 20% of tokens); the ratio is approximate, not an exact byte guarantee.

## Observation 5 (2026-09-17)

In the opencode_cc_mem repo (2026-09-17), 7 modified files split cleanly into logically independent commits: (1) fix(handover): keep In progress/To do whole on boot-budget overflow — handover.py (_split_items overflow rewrite, 87 lines), boot_context.py (token_budget 6000->10000 + docstring), versioning.py + tests/test_versioning.py (track .handover-state.md in the versioning repo gitignore), tests/test_handover.py; (2) feat(server): per-step boot timing to boot-timing.jsonl — server.py only (_BOOT_T0, _log_boot_step); (3) a tiny separate housekeeping commit for the root .gitignore hunk (uv.lock, known_problem/) which is unrelated to both. Rationale: separating the correctness fix from the instrumentation means reverting one will not disturb the other. CAVEAT: this was a recommendation made before the double-boot guard was added; if the guard is folded into commit (2) as proposed (same file, same boot path), the split stays valid but commit (2) grows.


---

[PROJECT: Double-boot root cause: lazy import of server.RULES_DIR from startup_scan re-executed server.py module body, spawning a second boot pipeline]
---
confidence: 0.9
created: '2026-09-17T10:55:23.926488+02:00'
description: On 2026-09-17 the compchem-memory server launched via `python -m compchem_memory.server`
  spawned TWO overlapping boot pipelines. Determining evidence (from storage.py/startup_scan
  docstrings + test_bo
id: '20260917_105523_926488'
observation_count: 2
observed_in_sessions:
- ses_f2df831f5ffetb3SnG8TDiGee9
- ses_f519131bcffeiCT6rN2Fq7S71c
opencode_session_id: ses_f519131bcffeiCT6rN2Fq7S71c
source: opencode_distill
tags:
- 892614a
- _claim_once
- boot_timing
- circular-import
- compchem-memory
- distill_timer
- double-boot
- double_boot
- magnolia-boot
- python-dash-m
- regression-verification
- resolved_rules_dir
- server.py
- startup_scan.py
- storage.py
title: 'Double-boot root cause: lazy import of server.RULES_DIR from startup_scan
  re-executed server.py module body, spawning a second boot pipeline'
tools:
- compchem-tools_run_shell
- git
- pytest
type: scientific_finding
updated: '2026-09-17T10:55:23.926488+02:00'
---

On 2026-09-17 the compchem-memory server launched via `python -m compchem_memory.server` spawned TWO overlapping boot pipelines. Determining evidence (from storage.py/startup_scan docstrings + test_boot_once_guard.py): the module is in sys.modules only as `__main__` under `-m`; startup_scan's lazy `from compchem_memory.server import RULES_DIR` (~14 s into boot, inside _maybe_promote) re-imported server.py under its canonical name, re-executing the module body and starting a second boot: two handover merges (83 s and 93 s) and two distill timers. A module-global boolean cannot guard this because the two module objects do not share globals. Fix (commit 892614a): `storage.resolved_rules_dir()` (MAGNOLIA_RULES_DIR env override or global fallback) becomes the single source of truth so startup_scan no longer imports server; `server._claim_once(name)` guarantees boot pipeline and distill timer run once per process, keyed by project dir, storing the claim in os.environ; plus `[boot-timing]` per-step ms logging. Regression tests: tests/test_boot_once_guard.py. CAVEAT: single-incident root cause established from timer overlap (two 83 s/93 s merges, two timers) and code inspection; the env-based `_claim_once` guard only protects once-per-process side effects when both code paths read os.environ, not module globals.

## Corroborating observations (merged)

- (session ses_f2df831f5ffetb3SnG8TDiGee9) Double-boot bug confirmed fixed: 10 single-pipeline boots since 2026-09-18

The double-boot (circular import) failure is verified fixed by fix 892614a committed 2026-09-17. All 10 boot 'total' entries in boot-timing.jsonl from 2026-09-18 onward show exactly one pipeline of 6 steps (server_import, startup_scan, handover, boot_context, audit, total) — no duplicate handover merges and no second distill timer. The 2026-09-17 incident (two overlapping pipelines at 83 s and 93 s) has not recurred. Determined how: enumerating 'total'/step entries per boot in boot-timing.jsonl. CAVEAT: verification covers only logged boots on this machine in the 09-18 → 09-24 window; a recurrence under different launch paths (e.g. magnolia-exp launcher) is not excluded.
