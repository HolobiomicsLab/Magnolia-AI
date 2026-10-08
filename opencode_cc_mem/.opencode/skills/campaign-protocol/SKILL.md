---
name: campaign-protocol
description: "Running a long autonomous scientific campaign through Magnolia (binder design, discovery sweep, any multi-stage effort of hours–days): campaign-brief structure (science 1/3, operations 2/3), budgets and pacing governor baked into the brief, role agents (worker/supervisor/curator/editor mapped onto daemon lanes), shared knowledge base + decision records, pre-launch metric calibration and tool validation, kill-tests and kill ledgers, verification before reporting. Use when starting or running an autonomous campaign (a campaign brief, a funnel of candidates, a CRO-style validation step). Do NOT use for single computations, one-shot docking runs, or the literature→idea-ticket loop (that is the daemon path)."
metadata:
  version: "0.1"
  last_verified: "2026-10-08"
  tags: "campaign,autonomy,pacing,supervision,kill-test,verification-before-reporting"
---

# Campaign protocol

How Magnolia runs a long autonomous scientific campaign — the discipline distilled
from Anthropic's two campaign papers (binder design, 2026-08-18, rank 1; ART enzyme
discovery, 2026-09-23, rank 2 — digests in the literature project,
`digests/anthropic_ai4s/`). The lesson of both papers: **the harness is the result**.
The campaign prompt, its gates, and its records are the science; the designs and
discoveries are the output.

Drafted 2026-10-08 (v0.1). Per the source digest's own rule: iterate this on a pilot,
then freeze.

## When to use / not use

Use: any autonomous effort with (a) hours–days of agent time, (b) a candidate funnel
(many designs/observations screened down), (c) a claim at the end. Not for single
computations, interactive sessions, or the literature sweep loop.

## 1. The brief — science 1/3, operations 2/3

The campaign brief is the only artifact the agents read. Anthropic's split:
one third science, two thirds operations. The operations majority is not bureaucracy —
it is where campaigns actually fail.

Science third:
- Target and objective in one paragraph each.
- Success criteria as numbers, and the claim's expected form ("a ranked list of 30
  designs with KD<10 nM", "3 confirmed novel associations").
- Calibration obligations (see §3).

Operations two-thirds:
- **Budgets baked into the brief**: wall-clock, money, concurrency, candidates per
  target. Anthropic: 48 h / $50 k multi-target, 24 h / $10 k single-target, 30
  designs per target. Magnolia: `submit_job` time_limit/ncores/memory per stage, and a
  run-record per stage (rules/job_execution.md).
- **Pacing governor**: screening volume, optimization rounds, and concurrency scaled
  to the budget. In Magnolia: the daemon's `schedule_days`, one task at a time per
  lane, and the campaign's own stage letters (open → next stage only when the gate
  says so).
- **Tool rule**: no pre-installed mystery stack. Each tool is built from its public
  source, and validated in the first hour (a known-answer run).
- **Supervision rule**: what a supervisor checks per stage (plan, summary, files),
  and who may open new tasks (see §2).
- **Reporting rules** (§5).

## 2. Execution model — roles, not a jury

Anthropic's campaigns are role pipelines, not parallel-assessor juries (the ART run:
949 sessions = 414 worker + 375 supervisor + 107 curator + 52 editor; 98/119 tasks
were agent-opened follow-ups). In Magnolia the roles map onto the lane machinery:

- **Worker** = a daemon lane session (headless, one letter at a time). Workers write
  plans and results, never the campaign-level claim.
- **Supervisor** = the review hop. The supervisor's distinctive power: it may open NEW
  tasks in response to observations (agent-opened follow-ups), each through a triage
  queue with an accept/reject + written reason. In Magnolia: follow-up letters with a
  `status:` lifecycle; the triage reason lives in the letter.
- **Curator** = the memory system itself: findings enter staging → confirmed entries,
  injected into later workers via `memory_get_context`. Same write-path as ART's KB.
- **Editor** = the human. Merge and publication decisions stay human.

Everything goes to the version-controlled record (run dirs, session JSONL, letters).
Parallelism exists for throughput, never for judgment — judgment is serialized through
the supervisor and editor.

## 3. Pre-launch gates — validate the instrument before the experiment

Do not launch until all four pass:

1. **Metric calibration.** The ranking score is validated on known positives AND
   negatives BEFORE the campaign (their example: co-folding ensemble benchmarked on
   Overath, macro-AP 0.66 vs AF3's 0.55). Magnolia analog: the replay-eval gate for
   memory changes; for chemistry, a known-binder sanity check against existing run
   data. A campaign with an uncalibrated metric is a campaign that will produce a
   number you cannot defend.
2. **Known-answer tool validation.** Every tool is run once against a case with a
   known answer, in the first hour.
3. **Pre-registered acceptance.** The pass/fail rule is written before the funnel
   runs — including an honest-negative clause ("if the only win is X, report
   negative; do not merge").
4. **Prejob checks** per rules/prejob_check.md for every stage's inputs.

## 4. Mid-campaign discipline

- **Funnel order: cheap before expensive.** Novelty/redundancy/composition filters
  before co-folding; lexical screens before LLM judges; LLM judges before physics.
- **Forced method diversity**: ≥3 independent generators, none >50% of the final set.
  Hedges single-method blind spots at near-zero cost.
- **Kill-tests before claims**: any novelty claim runs a dedicated falsification pass
  (ART: kill-test literature searches; the 14 rejected families are itemized by
  failure class — the kill ledger is a first-class artifact).
- **Decision records**: every stage's accept/reject/park decision lands in the run
  dir with its reason. The record, not the chat, is what the report cites.
- **Raw context for workers.** ART's fragility finding: the recognition event was
  lost in 10/10 reruns, and recognition dropped 90%→32% when tooling replaced raw
  context. Lanes must be able to read raw data (transcripts, spectra, sequences),
  not only structured summaries.

## 5. Verification before reporting

- External verification is blind: validators never see ranks, models, or campaign
  internals (their CROs were blind to each other and to the rank).
- The call rule is written AFTER data exists but applied UNIFORMLY to every candidate
  (post hoc rules tuned per-candidate are p-hacking in a lab coat).
- Failures are published alongside successes (they reported 0/90 next to the wins).
- Claims carry their caveat: what was measured, what was not, what remains untested.

## 4.5 Watch panel and epoch discipline

Settled by adversarial panel 2026-10-08 (verdict: `projects/xiulian/runs/2026-10-08_debate-campaign-watch-glm/verdict.md`). Two layers, two cadences, two authority levels:

**L0 — integrity layer (daemon-owned, no LLM, cannot be steered).** Wall-clock tick (~30 min) AND every stage boundary. Three signals earn STOP-THE-LINE (pause the stage, write a stop letter, resume only on editor ack — the stop is inert, never a state modification): instrument-pin mismatch (resolved corpus/config/judge/memory-state vs the epoch manifest), dead end-to-end known-answer check (a real judge call on a known accept/reject pair — a URL ping would have passed the judge-retirement incident), job-terminal violation (job past `time_limit`+margin; a `wait_for` parking line surviving a terminal record). Everything else (inflow rates, dup/dead-weight shares, recurrence counts, retrieval utilization) is ADVISORY: statistical counters with hand-set thresholds false-stop on noise, and a watch that halts science three times gets turned off.

**L1 — semantic layer (supervisor + editor, per stage gate).** Decision-record completeness; kill-ledger integrity; raw-context access check; retrieval fire→decision-change join (never raw fire counts); the standing anomaly question.

**Epoch manifest** (`campaign-epoch.json`, per stage — no manifest, no stage start): Magnolia commit, **memory-state hash** (same code + different memory state is a different instrument), corpus id/hash, judge model id, config hash. Infra changes land ONLY at stage boundaries, as classified letters into the campaign inbox — plumbing (dead-instrument hotfix) / gate (threshold or predicate) / guidance (retrieval behavior) — each stating its expected direction of effect on the watched counters. Gate-like plumbing (judge swap) requires known-answer re-certification before the stage resumes. Chemistry/acceptance gates are frozen in the brief and never amendable by a Magnolia-side letter. No self-measurement: a Magnolia change is evaluated on the frozen-corpus replay eval or the next campaign — never by the campaign it runs under.

**Watchlist** (`watchlist.md`): rows typed `observation` or `gate`. Gate rows frozen per epoch. Observations grow at any gate via the standing anomaly question — asked worker-first, cold, about the work ("what did you see that was not in the brief?"), from raw context; the worker never sees the watch; the answer reaches the supervisor only after the supervisor's own answer is written (write order enforced, not promised). Empty answers are valid, counted, and the empty-rate is reported. Hard cap ~12 active items; never-fired items get mandatory review. New counters that read zero while their channel shows traffic are an integrity alarm, not success.

**Bias ledger** (`bias-ledger.md`): one row per change — date, epoch, commit delta, subsystem, hypothesized bias direction, counters affected, decision. Claims spanning epochs are marked contaminated unless they survive a rerun under one config. Noise band: measured pre-launch (N≥3 unchanged-instrument runs on the frozen corpus) for the corpus metrics, and sampled by the L0 tick for live counters (qualitative until epoch 2 — labeled as such).

## Common mistakes

| Mistake | Correct |
|---|---|
| Brief is all science, no operations | science 1/3, operations 2/3 — budgets, pacing, supervision, reporting |
| Launch, then calibrate the metric | calibrate on known positives/negatives first |
| Ranking directly on an expensive assay | cheap filters → expensive scoring → blind verification |
| One generator/method only | ≥3 methods, none >50% |
| Successes reported, failures dropped | the kill ledger is a first-class artifact |
| Workers see only tool summaries | give lanes raw context; tooling-only recognition collapses |
| Parallel agents for judgment | parallelism for throughput; judgment stays serialized (supervisor → human) |

## When this skill is wrong

- Anthropic changes the protocol (new campaign releases) — re-digest before reuse.
- A campaign short enough that the 2/3 operations overhead exceeds its value — then
  use plain `rules/job_execution.md` + `prejob_check.md`.
- The pilot phase: freeze only after one pilot round has surfaced this skill's gaps.
