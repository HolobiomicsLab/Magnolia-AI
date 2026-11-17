# Live pass 3, attempt 2 (commit 82a7e15) — sanitised evidence

Gate paths are `<G>`, the outside credential dir `<OUTSIDE>`, the home `~`. Key names only; no values.
Attempt 1 (f587745, zero model calls) is kept under `attempt1/` in the plan folder copy.

| File | Content |
|---|---|
| dry-runs.json | d1: production + candidate executor_for for Claude/Codex, identity diagnostic, OpenCode routes; d2: Codex after trust |
| capabilities-p3.candidate.json | candidate table: production with rows[4].stale and rows[5].stale cleared (sha256 9a165a3b…) |
| codex-hooks-list.json | production `_fresh_codex_hook_rows` rows before/after hook trust; campaign probe launches |
| codex-config.json | `~/.codex/config.toml` journal: trust entries added, cmp-proved restore |
| run2-worker-claude-code-review.json | run 2 state, events, worker result, review, receipt, hook rows, ledger rows, tree diff |
| spawns.json, budget.json | every client/memory spawn with env KEY NAMES; per-client counts |
| openrouter-calls.json, memory-extraction.json | per-call memory route log and cost; llm-timing, session/staging names |
| campaign-routes.json, protocol-diff.txt, opencode-models.json | OpenCode-on-OpenRouter preparation (not run) |
| version-checks.txt, nonmodel-launches.txt, owner-state.json, operator-notes.txt | checks and operator notes |

## Continuation (11:17 to 11:42 CET)

| File | Content |
|---|---|
| cont-run1-decline.json | run 1 finish: state-API decision (waiting_human -> completed), runner pass, `distill-queue` result, final analysis |
| cont-run2-worker-codex-review.json | `worker-codex-review`: analysis, runner result, the worker's first memory call, operator hooks/list x1/x2, config journal, spawns, memory calls |
| cont-run3a-opencode-control.json | `opencode-control` on OpenRouter: analysis, OpenCode tool calls, routes, the gate OpenCode config override (baseURL + provider prefs), proxied calls, key usage counters |
| cont-run3b-opencode-reviewer-only.json | `opencode-reviewer-only`: analysis (incl. the reviewer JSON), first-launch refusal, `service stop`, OpenCode tool calls, proxied calls |
| cont-run3c-opencode-helper-evaluator.json | OpenCode helper (agents-daemon `_run_once`) and evaluator (`GenerativeEvaluator._call_client`), ledger rows, calls |
| cont-totals.json | budget counters, full usage ledger, every proxied call with spawn attribution, key usage counters (numbers only), version checks, non-model launches, owner-state listings, operator notes |
