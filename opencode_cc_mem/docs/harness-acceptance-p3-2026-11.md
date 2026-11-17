# Plan 3 harness acceptance record — 17 November 2026

This is the acceptance record for the final working tree based on `4a2233c`.
The binding packet forbids Git-state changes, so the final fold is intentionally
uncommitted. `observed` means that a sanitised live artifact records the stated
outcome; it does not turn a failed observation into acceptance. `refused` means
the production capability gate remains closed. `deferred` means no accepted
live role was established. No raw client export, credential, private prompt,
home path, user name or scientific secret is published.

## Client and role matrix

<!-- acceptance-matrix:start -->
| Client | Role | Status | Evidence |
| --- | --- | --- | --- |
| opencode | worker | observed | opencode_cc_mem/docs/evidence/live5/c3-opencode-control.json |
| opencode | reviewer | observed | opencode_cc_mem/docs/evidence/live5/c4-opencode-reviewer-only.json |
| opencode | helper | observed | opencode_cc_mem/docs/evidence/live3/cont-run3c-opencode-helper-evaluator.json |
| opencode | evaluator | observed | opencode_cc_mem/docs/evidence/live3/cont-run3c-opencode-helper-evaluator.json |
| claude-code | worker | observed | opencode_cc_mem/docs/evidence/live5/c2-worker-claude-code-review.json |
| claude-code | reviewer | observed | opencode_cc_mem/docs/evidence/live5/c1-worker-codex-review.json |
| claude-code | helper | observed | opencode_cc_mem/docs/evidence/live2a/cont-runs.json |
| claude-code | evaluator | observed | opencode_cc_mem/docs/evidence/live2a/cont-runs.json |
| codex | worker | observed | opencode_cc_mem/docs/evidence/live5/c1-worker-codex-review.json |
| codex | reviewer | observed | opencode_cc_mem/docs/evidence/live5/c2-worker-claude-code-review.json |
| codex | helper | refused | opencode_cc_mem/docs/evidence/codex/task-codex-0169-audit-report.md |
| codex | evaluator | refused | opencode_cc_mem/docs/evidence/codex/task-codex-0169-audit-report.md |
| vibe | worker | deferred | opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| vibe | reviewer | deferred | opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| vibe | helper | deferred | opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| vibe | evaluator | deferred | opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
<!-- acceptance-matrix:end -->

The OpenCode helper produced `ANSWER: 42`; its evaluator launched but returned
no usable JSON decision. Those are observed outcomes, not a promotion of the
evaluator. Codex helper and evaluator still fail closed. The final-tree identity
check found every recorded component unchanged from pass 5: Codex worker and
reviewer are therefore current (`stale: false`), while their helper/evaluator
remain stale and refused. All four Claude identities remain equal to live 2a;
the worker and reviewer campaign cells now cite pass 5. Vibe rows are unchanged.

## Five live passes

| Pass | What it established | What it found |
| --- | --- | --- |
| 1 (including 1b) | Codex 0.169.0 worker/reviewer boundaries and usage; Claude 2.1.326 worker, reviewer, helper and evaluator identities and policies | Claude had auto-updated from the bound version; Codex memory readiness was not yet demonstrated; the campaign fixtures were not initially runnable |
| 2a | Current Claude identities, a complete Claude worker/reviewer continuation, OpenCode worker safety, and genuine `waiting_quota` handling | The free OpenCode reviewer hit HTTP 429 with `reset_at` missing; OpenCode campaign memory was absent; provider extraction failure could be silently sealed |
| 3 | Fresh Codex hooks/list (A8), Codex worker boundaries, OpenCode worker/reviewer/helper/evaluator routes, OpenRouter usage and owner-state preservation | Cached input exhausted the protocol budget; the model-supplied project path broke first memory access; human decline lacked a production path; the evaluator returned no JSON; empty dry-run sessions caused unnecessary extraction calls |
| 4 | Vibe 3.1.2 binary identity, rendered hooks, telemetry-off environment and fail-closed production refusals without a model call | Persisted trust required owner authorization and five technical gaps prevented an accepted Vibe role |
| 5 | Four end-to-end campaigns passed: Codex→Claude, Claude→Codex, OpenCode control, and Claude→OpenCode; decline-followups, continuity drain-once, memory readiness, boundaries and H1 campaign accounting held | Ledger rows still omitted H1 uncached/cache fields; OpenCode retained its default title `small_model`; the evaluator prompt issue remained a proposal |

Pass 5 recorded one physical usage row per client call, successful first memory
access for the Claude/Codex campaign workers, denied secret/out-of-scope writes,
valid first-try reviews, audited human decisions, and drains that completed once
then returned zero sessions. Live 3 and 5 evidence was rescanned during this
fold. It already used `<G>`, `<OUTSIDE>` and `~`; no additional replacement was
needed. Live 3 `attempt1/` was deliberately excluded because no final cell cites
it.

## Defects found live and final disposition

- A8 fresh Codex hook discovery was implemented and observed in passes 3 and 5.
- F-L1/F-L8 required servers and project binding were corrected; pass 5's first
  memory calls succeeded even with model-supplied project arguments.
- F-L2/F-L3 review-followup shape and evidence-path validation were corrected.
- F-L4 provider failures no longer seal a session. This final batch adds a
  bounded, persistent per-session retry schedule; exhausted sessions remain
  unsealed and visible instead of retrying on every startup.
- F-L5 empty or boot-only sessions now seal as empty without an LLM call.
- F-L6/H1 charges uncached input, cache creation, output and reasoning while
  reporting cache reads separately. This final batch also persists nullable
  `uncached_input_tokens`, `cache_read_tokens` and `cache_creation_tokens` in
  usage-ledger rows and exposes them through `magnolia usage`.
- F-L7/M1 added an audited `decline-followups` transition that writes a passed
  receipt and queues continuity only after the stage and review gates pass.
- m-k1 now shares the setup-probe predicate: every case-insensitive
  `*_API_KEY` is removed from Claude, Codex and Vibe client environments, while
  selected keys still reach the managed memory server. OpenCode runtime
  behaviour is unchanged.
- OpenCode helper/evaluator routing was restored to the base contract, but the
  live evaluator still showed that its JSON prompt/step shape needs a separate
  product decision.

No capability-row identity component was changed by the minor batch or evidence
fold. The table promotion changes only Codex worker/reviewer `stale` state and
evidence citations.

## Budgets and cost

| Resource | Campaign total |
| --- | ---: |
| Claude model calls | **31/40** |
| Codex model calls | **34/44** |
| OpenCode live calls | **10** (2 in pass 2a, 5 in pass 3, 3 in pass 5) |
| Vibe model calls in pass 4 | **0** |
| OpenRouter pass 3 | **about US$0.038** |
| OpenRouter pass 5 | **US$0.0284** (`$0.0086` memory + `$0.0198` OpenCode) |
| OpenRouter tracked campaign total | **about US$0.0664** |

The pass-5 key counter rose by `$0.0220`, less than the proxy's `$0.0284`
per-response sum because the counter lagged the final calls. Both measures were
well below the authorized pass cap.

## Vibe deferral

No Vibe role is accepted. The five gaps are:

1. Normal persistent project trust lives in `~/.vibe/trusted_folders.toml` and
   required an owner-authorized write or interactive trust action.
2. Magnolia lacks a Vibe branch in installed-client version binding, so a bound
   launch identity is refused.
3. D3 memory cannot be enabled by a table change alone: Vibe renders no managed
   memory server while `vibe_memory` remains unobserved.
4. D4 worker editing is hard-refused in the registry/adapter and its edit tools
   are disabled in argv; process tools also need a deliberate policy.
5. The telemetry-off free model disappeared in the earlier probe. A supported
   OpenRouter route needs Magnolia-rendered provider configuration without
   forwarding a provider key to the subscription client.

## Open proposals for Tao

- Add an OpenCode role deny for `.magnolia/services/**`; it was intentionally
  not applied because OpenCode agent Markdown and golden snapshots are frozen.
- Give the OpenCode evaluator a tested JSON-producing prompt/step contract, or
  keep that role explicitly unavailable.
- Pin OpenCode `small_model` so session-title calls do not silently use the
  default `openai/gpt-6-luna` route.

## Parked minors

The remaining non-blocking items stay parked: the OpenCode setup probe still
filters provider keys although OpenCode runtime does not; critic handoff can
lose configured-project fallback sessions; managed-service liveness lacks a
start-token proof; one project-binding test does not traverse the complete
mounted middleware chain; a dead service with a leaked live holder can remain
fail-closed until release; some non-route service flags are outside the reuse
digest; first-boot boot-context can be absent during a busy no-handover window;
session-source enumeration has a file-disappearance race; and read-only usage
reporting/CLI argument hygiene retain the previously recorded conservative
limitations. None is promoted by this record.

## Final verification contract

The authoritative verification is the complete campaign and memory suites from
both the source tree and an rsync-relocated copy, with invocation-local
`--basetemp` directories removed after the run. Exact counts and any known
baseline/sandbox failures are recorded in `FINALFOLD-REPORT.md`; this document
does not convert an unavailable or failing check into a pass.
