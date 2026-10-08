## Done

- **ASTRA adaptor built on `exp/opencode-v2` (`03aba0f`).** `astra_model.py` vendored verbatim from Ad Verum PR #2 (`891c5cd`, lfnothias; comment-only header, upstream preserved). `astra_export.py` converts Magnolia decision receipts → ASTRA 0.0.12 `Analysis` docs (decisions→Decision+Options, command→Output+Recipe, outcome→derived Insight w/ run-record evidence, args→Inputs); writes `<run_id>.astra.yaml` to `.magnolia/astra/` (never `runs/`). 4 tests + fixture; v2 suite 694 pass. Caveat: model built from the commit, not yet run through lfnothias' `astra validate`.
- **Branch split done (user decision).** `exp/distill-admission` = pure v1 (`7f6c70d`, daemon has zero v2 refs, 21 daemon tests pass). `exp/opencode-v2` = superset: merge `da72cfd` (doorbell conflict resolved: dual-shape + registry mentions) + v2 execution mode re-homed `fd9827f`.
- **Both branches pushed to origin** (`HolobiomicsLab/Magnolia-AI`): `exp/distill-admission` → `8da90bc..7f6c70d`; `exp/opencode-v2` new branch. (First attempt hit a transient GitHub 500; retry landed.)
- **Daemon build (`3913ddc` on exp).** Registry lanes (`agent.json`), `wait_for: job:<run_id>` park/re-deliver, scheduled sweeps, persistent lane sessions (`--session` resume), doorbell registry mentions; 21 tests pass. Lane registrations + ticket example are local (`projects/` gitignored).
- **Semantic recall scorer (`b2cd45e`) + report wiring (`4415b5b`).** bge-m3 via ollama (TF-IDF fallback). Arm3: lexical 67% vs **semantic 100%** (36/36 workhorses ≥0.6); TF-IDF probe recovered 10/10 "missing". Report shows both; pre-registered verdict unchanged (Goodhart guard).
- **v6-repair arm3 verdict (run `2026-10-07_081745_hsc70-arm3-v6repair`).** 646/646 slices, 1659 candidates. vs arm2: volume 54.5%→43.6%, dup 7→0, dead-weight 72% PASS, recall 67% lexical (scorer artifact). `RECALL-ANALYSIS.md` + probe archived in run dir.
- **Judge certification (`certify_labels.py`).** 29/30 human-rejected pairs suppressed by hardened judge, 0 catastrophic (≥0.8) misses; accept-side unmeasured (sources consumed by merges).
- **Boot-regression investigation.** Not backlog-driven — handover flat ~36–41 s; >110 s boots are the double-boot bug. Main-model stamp added to boot-timing `total` rows (`6a9289c`).
- **Test hermeticity fix (`6a9289c`).** Daemon-exported `MAGNOLIA_*` flags leaked into pytest → 25 spurious failures incl. the 2 "pre-existing" ingest ones. Autouse conftest strip on both trees; suites fully green (679 exp / 690 v2).
- **gitignore cleanup (`e8c3d0d`, `8862f38`, `5fdda4b`, `250dda2`).** Untracked egg-info, ignored replay-eval `runs/` + daemon state + corpus bulk (transcripts/exports/slices); corpus versioning files (manifests, labels) now tracked. Both trees at 0 untracked.
- **Daemon-agents plan doc (`8b20a02`, `1c07a3b`, `ec82468`).** STE rewrite; single activator daemon, xiulian as registry lane, idea-ticket schema, `wait_for` re-delivery.
- **v2 verification matrix.** Unit suites green; dual-shape plugins proven on both runtimes; memory MCP (stdio) + compchem-tools MCP (8012) both live under v2 client; capture→distill chain verified live; Code Mode nested hooks carry full tool names (`6619b63`).
- **Earlier (carried):** v2 home + config render + port isolation; `reconstruct_transcript` v2 branch; in-process capture via `session_message` (`dc8168f`, `b3f1902`); launcher profiles (`4e3c0e4`); set -e fix (`bd87640`); floor + label store (`d204e13`); R7 auto-confirm (`8f1e586`); retirement v2 (`b938663`); seeding/sibling/evidence (`4dd707d`); A2 backfill (`0f242fe`); prose-number fix (`6be98d1`); v6 prompt repair (`c94e2d2`).

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

## Stale?

- **opencode-go model-definition verification** — superseded by the 2026-09-11 panel update + explicit-ID verification. (no activity)

## Key files

- `~/repos/project_magnolia-v2` — v2 worktree, branch `exp/opencode-v2` (`03aba0f`); superset of exp + v2 work; venv `.venv`.
- `~/repos/project_magnolia-v2/opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/astra_model.py` — vendored ASTRA 0.0.12 model (from Ad Verum `891c5cd`).
- `~/repos/project_magnolia-v2/opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/astra_export.py` — receipts→ASTRA converter; `tests/test_astra_export.py`.
- `~/repos/project_magnolia-v2/opencode_cc_mem/softwares/bin/magnolia-agents-daemon` — v2 execution mode (`MAGNOLIA_OPENCODE_V2`); registry/wait_for/schedule/lane sessions.
- `~/repos/project_magnolia-exp/opencode_cc_mem/softwares/bin/magnolia-agents-daemon` — pure-v1 daemon (`7f6c70d`).
- `~/repos/project_magnolia-exp/opencode_cc_mem/.opencode/plugins/magnolia-agent-doorbell.ts` — registry-based mentions (v1).
- `~/repos/project_magnolia-v2/opencode_cc_mem/.opencode/plugins/` — 6 dual-shape plugins (doorbell = dual-shape + registry).
- `~/repos/project_magnolia-exp/opencode_cc_mem/replay_eval/recall_scorer.py` — bge-m3 semantic recall scorer (TF-IDF fallback).
- `~/repos/project_magnolia-exp/opencode_cc_mem/replay_eval/bakeoff_report.py` — five-gate report + semantic recall line.
- `~/repos/project_magnolia-exp/opencode_cc_mem/replay_eval/certify_labels.py` — judge-vs-labels certification.
- `~/repos/project_magnolia-exp/opencode_cc_mem/replay_eval/runs/2026-10-07_081745_hsc70-arm3-v6repair/` — arm3 run + `RECALL-ANALYSIS.md` + `RECALL-SEMANTIC.json`.
- `~/repos/project_magnolia-exp/opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/receipts.py` — decision-receipt extractor (P1).
- `~/repos/project_magnolia-exp/opencode_cc_mem/docs/daemon-agents-plan.md` — STE plan (registry activator, idea tickets, build order).
- `~/repos/project_magnolia-exp/opencode_cc_mem/rules/inbox.md` — inter-project inbox protocol + idea-ticket schema.
- `~/repos/project_magnolia-exp/opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/opencode_ingest.py` — dump-first exporter chain + `v2_db_exporter` (session_message).
- `~/repos/project_magnolia-exp/opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/server.py` — boot-timing main-model stamp (`6a9289c`).
- `~/repos/project_magnolia-exp/opencode_cc_mem/mcp-servers/compchem-memory/tests/conftest.py` — hermetic MAGNOLIA_* env strip.
- `~/repos/project_magnolia-exp/opencode_cc_mem/projects/xiulian/.magnolia/reflex/labels.jsonl` — 33 rows.
- `~/repos/project_magnolia-exp/opencode_cc_mem/projects/xiulian/.magnolia/admission-log.jsonl` — 58 rows.
- `~/repos/project_magnolia-exp/opencode_cc_mem/projects/xiulian/.magnolia/action-retrieval.jsonl` — 58 new-schema rows.
- `~/repos/ad_verum` — Ad Verum repo; ASTRA Run schema at commit `891c5cd` (`feat/astra-run-provenance`, PR #2); `indicium/` package; `scripts/sync-magnolia.sh`.
- `~/.local/share/magnolia-v2/bin/opencode` — pinned v2.0.6 binary.
- `~/.local/share/opencode/opencode-v2.db` — v2's own DB (never the shared v1 DB).
- `~/.local/share/opencode/opencode.db` — 1.2 GB, SHARED by v1.18.34 and v2.0.6 (v2 probe must set `OPENCODE_DB`).
- `~/.bashrc` — holds `ALBERT_API_KEY` (158 chars); returns early for non-interactive shells — extract the line directly, never echo it.

## Won't-do / Archived

- **Issue #9 (reproducible evidence bundle) — REJECTED BY DESIGN (2026-09-11).** Magnolia is designed not to upload memory entries; the repo carries the assistant, not the science. User: "ignore that." Issue left open on GitHub (no closing comment). Recorded as a design constraint so it is not re-proposed.
- (none)
