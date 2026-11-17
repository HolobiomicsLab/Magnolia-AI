# Plan 3 harness acceptance draft — 17 November 2026

This is the offline acceptance record at `912aec5` (first drafted at
`c38d1fa`). It is deliberately not a
phase-release decision. `observed` means a tracked, sanitised live artifact
exists; `stub-only` means only offline or synthetic proof exists; `refused`
means the production capability gate rejects the role; and `pending` means a
live re-observation or prerequisite is still owed. No raw client export,
credential, private prompt, home path or scientific secret is published here.

## Client/role disposition

<!-- acceptance-matrix:start -->
| Client | Role | Status | Evidence |
| --- | --- | --- | --- |
| opencode | worker | stub-only | opencode_cc_mem/tests/test_opencode_compatibility.py |
| opencode | reviewer | stub-only | opencode_cc_mem/tests/test_opencode_compatibility.py |
| opencode | helper | stub-only | opencode_cc_mem/tests/test_agents_daemon.py |
| opencode | evaluator | stub-only | opencode_cc_mem/tests/test_evaluator.py |
| claude-code | worker | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| claude-code | reviewer | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| claude-code | helper | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| claude-code | evaluator | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| codex | worker | pending | opencode_cc_mem/docs/evidence/codex/gate4/README.md<br>opencode_cc_mem/docs/evidence/codex/gate4/launch-identities.json |
| codex | reviewer | pending | opencode_cc_mem/docs/evidence/codex/gate4/README.md<br>opencode_cc_mem/docs/evidence/codex/gate4/reviewer-policy.json |
| codex | helper | refused | opencode_cc_mem/docs/evidence/codex/task-codex-0169-audit-report.md<br>opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| codex | evaluator | refused | opencode_cc_mem/docs/evidence/codex/task-codex-0169-audit-report.md<br>opencode_cc_mem/campaign_runner/executors/capabilities-p3.json |
| vibe | worker | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/docs/harness-neutral-agents-plan-p3.md |
| vibe | reviewer | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/docs/harness-neutral-agents-plan-p3.md |
| vibe | helper | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/docs/harness-neutral-agents-plan-p3.md |
| vibe | evaluator | pending | opencode_cc_mem/docs/harness-probes-p3-live-2026-11.md<br>opencode_cc_mem/docs/harness-neutral-agents-plan-p3.md |
<!-- acceptance-matrix:end -->

Claude Code 2.1.325 previously passed worker, reviewer, helper and evaluator
gates, and the final-launch re-run covered worker/helper/evaluator. Task 14 then
changed the bound `hook_source` digest from `37e00cb7…` to `0cb4cea4…`, and
fix B (server-side memory allowlist, writer lease) moved it to `927d5975…`,
while preserving the gate rows. The earlier behavior remains historical observation,
but all four current identities are marked **re-observation pending** here.

Codex CLI 0.169.0 worker and reviewer were observed at gate 4 with model
`gpt-5.6-luna`, normal persisted hook trust, allowed reads/worker edits, denied
secret/outside/shell/delegation operations, and one subscription receipt per
physical invocation. The later package binding added the code-mode host,
manifest, exec-server and packaged `rg` hashes without changing the gated
configuration digests. Its residual is explicit: the code-mode catalog is
wider than the role allowlist. `write_stdin`, MCP-resource and
`multi_agent_v1__*` tools remain visible; the worker also sees non-allowlisted
memory tools. All exercised inner calls were hook-denied, while `write_stdin`
is unhooked but has no reachable session because `exec_command` is denied.
Acceptance therefore rests on the required deny-unknown hook, not catalog
removal. Fix B then trimmed the worker memory catalog to the role allowlist and
added token-state denials, which changed the worker `mcp`, `native_files` and
`policy` digests, the reviewer `policy` digest and both configuration
identities. Gate 4 is therefore historical observation of the pre-fix-B
identity, and both roles are **re-observation pending** here. Codex
helper/evaluator were not re-run on 0.169.0 and remain refused.

Vibe 3.1.2 has earlier adapter observations, including a paid fallback to
Mistral Medium 3.5 when telemetry was disabled. No current Plan 3 role has a
tracked accepted gate. The intended `zai-glm-5-3-flash` route in the protocol
fixture is a plan, not proof of actual model delivery; every Vibe role remains
pending and production-gated.

## Proof-table cells

| Proof row | Offline status and artifact | Live status and limitation |
| --- | --- | --- |
| Client/version/role identity | **stub-only** — capability validation and immutable bindings in `tests/test_executor_capabilities.py` | Codex worker/reviewer observed at gate 4 on the pre-fix-B identity, current identity **pending**; **pending** for current Claude identities and all Vibe roles; **refused** for Codex helper/evaluator |
| Worker usefulness and boundary | **stub-only** — role-policy, launch and read-server suites | Codex XW observed at gate 4 (pre-fix-B), current **pending**; Claude historical observation is **pending** re-observation; Vibe is **pending** |
| Reviewer independence and boundary | **stub-only** — family selection/rejection and no-edit tests | Codex XR observed at gate 4 (pre-fix-B), current **pending**; current Claude is **pending**; OpenCode/Vibe campaigns are **pending** |
| Rules/boot/hook delivery | **stub-only** — renderer parity and recorded fixtures | Claude has historical observation but current binding is **pending**; Codex XW/XR observed at gate 4 (pre-fix-B), current **pending**; OpenCode/Vibe are **pending** |
| Continuity and memory | **stub-only** — harness-qualified transcript, origin and drain-once tests | No complete mixed-client acceptance campaign exists: **pending**. Vibe D3 is also **pending** |
| Usage/governor | **stub-only** — admission, nullable counters, resume deltas and `magnolia usage` tests | Codex gate 4 and historical Claude runs have individual receipts, but shared day/week campaign proof is **pending** |
| Quota for all four caller kinds | **stub-only** — 56 synthetic stream cases in `tests/test_executor_acceptance_matrix.py`; synthetic labels are retained | Real account quota/reset evidence is **pending** for every client and is never promoted from synthetic data |
| Helpers/evaluator | **stub-only** — direct script/import/routing/sender/model/role tests preserve no-tools helper and plain evaluator output | Claude historical gates are **pending** re-observation; Codex is **refused**; Vibe is **pending** |
| Exempt boundaries | **stub-only** — Task 13 proves interactive and API memory/critic calls acquire no subscription slot | Jev's direct credential path remains unchanged and documented for Tao; no live acceptance is claimed |
| Refused/conditional roles | **stub-only** — production projection tests fail closed | Codex helper/evaluator are **refused**; Vibe roles and Vibe memory are **pending**, not passed |

## Bounded campaign protocols and invocation plan

The five schema-valid, single-stage drafts are under
`tests/fixtures/campaigns/harness-p3/`. Their shared prompt is
`prompts/prompt.md`, the asset layout copied by campaign `init`. Each asks for
`work/result.txt`; the runner, not the prompt, writes the normal stage receipt.
A reviewer is the stage's review phase, not a second stage.

| Protocol | Planned worker | Planned reviewer | Current disposition |
| --- | --- | --- | --- |
| `worker-claude-code-review.json` | Claude Haiku | Codex `gpt-5.6-luna` | **pending** current Claude identity re-observation |
| `worker-codex-review.json` | Codex `gpt-5.6-luna` | Claude Haiku | **pending** current Claude reviewer re-observation |
| `worker-vibe-review.json` | Vibe `zai-glm-5-3-flash` | Claude Haiku | **pending** Vibe gate and actual-model confirmation |
| `opencode-control.json` | OpenCode DeepSeek V4.1 Flash free | OpenCode GLM 5.3 Flash free | **pending** live control |
| `opencode-reviewer-only.json` | Claude Haiku | OpenCode DeepSeek V4.1 Flash free | **pending** current Claude identity and OpenCode reviewer control |

No protocol was run in this offline task. This task added **zero** client
invocations and **zero** memory/critic API calls.

## D1–D4 and OpenCode compatibility

- **D1 implemented:** defaults are 2 concurrent, 50 physical invocations/day,
  60 turns/invocation and 30 seconds between starts, shared per subscription
  key across projects for one OS user. Fifty is a ceiling, not a target.
- **D2 accepted for Codex worker/reviewer only:** gate 4 supplied the observed
  full-guarantee evidence on the pre-fix-B identity, with the catalog residual
  above; the current identity needs re-observation. No reduced-guarantee
  exception was used. Codex helper/evaluator remain refused.
- **D3 implemented in code but not accepted live for Vibe:** the project-bound
  loopback HTTP service exists, with the server-side tool allowlist (stdio,
  HTTP, catalog and hook in parity), the project writer lease and the
  token-state denial for Claude, Codex and Vibe roles covered by tests. The
  live read-denial probe and an authenticated Vibe run remain pending. Vibe memory stays disabled for acceptance purposes.
- **D4 approved but not enabled by evidence:** guarded Vibe worker edits are
  permitted by design only after the complete safety gate; that gate is pending.

OpenCode keeps its previous worker/reviewer retry, dedicated-service, parser,
reviewer DB fallback, helper and evaluator behavior. The release-visible
governor changes are exactly three: **(1) admission/daily-limit waits, (2)
governor caps, and (3) quota-stop behavior**. They do not add helper/evaluator
agent flags or turn OpenCode's frozen plugins into policy shims.

For Tao's review, propose—but do not apply—an OpenCode role deny for
`.magnolia/services/**`. Mode `0600` protects against other OS users, not a
same-user role; the deny plus a real read-denial probe is the relevant D3
condition. This task changes no behavior code or OpenCode plugin.

## Tao TM-3–TM-6 corrections

- A universal `max_price_per_run` was the wrong abstraction and is removed.
  Optional client-native budgets map to Claude `--max-budget-usd` and Vibe
  `--max-price`; Codex 0.169.0 has no price flag and is not refused for that.
- `claim-critic.ts` only hands turn-end work to Python. Python owns provider
  routing, sticky session identity, scrubbing and usage accounting. A cache
  breakpoint is justified only for a prefix that is actually reused.
- “At most one restart” means one fresh start after one failed reuse check per
  acquire, with no respawn loop. A health-probe retry is not a process restart.
- A token file at `0600` is not same-user role isolation. The OpenCode deny
  proposal above is not applied; the Claude, Codex and Vibe roles carry the
  deny in code and tests, and the live Vibe read-denial probe remains pending.
- Explicit OpenRouter routes fail visibly; they must not silently switch to a
  direct key. A separately configured fallback or visible heuristic/disabled
  degradation needs its own decision and test.
- New setups now write `memory_provider=openrouter` with
  `deepseek/deepseek-v4.1-flash` when `OPENROUTER_API_KEY` is available, so
  “OpenRouter is the default” holds for new configurations only. Existing direct DeepSeek, Kimi
  and Anthropic configurations are never silently migrated.

## OpenRouter privacy and direct-cost comparison

Memory selection may send the task description and selected candidate text;
distillation may send a bounded transcript; the critic may send the report and
bounded tool trace. These can contain unpublished scientific material even
after generic secret scrubbing. OpenRouter receives that content and may route
it to the pinned or selected upstream provider. The sticky identifier contains
only role plus a project-path digest, not the path. Raw exports, credentials and
bearer tokens are excluded from prompts and evidence. Before production use,
the owner must accept the gateway/upstream retention and zero-data-retention
terms appropriate to the chosen provider; this repository does not prove them.

For the same DeepSeek V4.1 Flash model, direct cost is
`uncached_input × direct_input_rate + output × direct_output_rate`; routed cost
is `uncached_input × router_input_rate + cache_reads × router_cache_rate +
output × router_output_rate`. The tracked campaign measured OpenRouter cache
reads of 12,416/12,489 prompt tokens for a repeated critic call and 2,560/2,661
for the extractor's second pass. It contains no matched direct-route run or
versioned unit-price snapshot, so it does **not** prove that either route is
cheaper. OpenRouter can win when those cache hits and provider availability
outweigh its current unit rates; direct can win when rates are lower or prompts
do not reuse a cacheable prefix. Record the dated unit prices, expected call
volume and a separate API budget before choosing. Memory/critic API billing is
outside the subscription governor in both cases.

