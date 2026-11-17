# Plan 4 (Mistral Vibe) acceptance record — 18 November 2026

Branch `feat/harness-vibe-p4`, plan `docs/harness-neutral-agents-plan-p4-vibe.md`. Offline Tasks 1–5 were
committed `96f7da7..1ba027b`; the live gate ran on a snapshot of `1ba027b`. Evidence is sanitised and lives
under `docs/evidence/live-p4/`. No credential, private prompt, home path or user name is published.

## Outcome

**No Vibe role is accepted.** Vibe 3.1.2 cannot remove its filesystem builtins (shell, `write_file`, `edit`)
from the model-visible tool catalogue. `enabled_tools`, `disabled_tools` and `--disabled-tools` only turn a
filtered builtin into a denial when it is called. Magnolia requires a role's forbidden tools to be absent, not
only denied, so all four Vibe rows stay refused. The cause is in Vibe's design, not in Magnolia's tool names:

- `vibe/app_server/_runtime.py:2088` enables the Core filesystem feature unconditionally;
- `vibe/app_server/_runtime.py:3108-3113` maps a filtered tool to execution mode `deny`;
- `mistralai_vibe_local_harness/vibe/_local_actions.py:1659-1669,760-772` sends Core's catalogue to the
  completion call without that filter.

Only subagents (`enable_subagents`) and background processes (`enable_background_processes`) have feature
switches that remove their tools. The live reproduction (two Vibe calls) showed the same seven-tool catalogue
with short-name filters and with `disabled_tools = ["*"]`; the model called shell and `write_file` and Vibe
denied both at execution. Revisit when an upstream release removes filtered builtins from the catalogue.

**Five Claude Code and Codex rows made stale by the Plan 4 hook-adapter change are current again.** The
change touched only `hook/adapters/vibe.py`, but `hook_source` hashes the whole hook tree.

## Matrix (Plan 4 changes only)

| Client | Role | Status | Evidence |
| --- | --- | --- | --- |
| claude-code | worker | re-observed, current | `live-p4/reobserve/B-cw*.json`, `live-p4/live6b/c1-claude-worker-*.json` |
| claude-code | reviewer | re-observed, current | `live-p4/reobserve/B-cr*.json` |
| claude-code | helper | re-observed, current | `live-p4/reobserve/B-ch*.json` |
| claude-code | evaluator | re-observed, current | `live-p4/reobserve/B-ce*.json` |
| codex | worker | re-observed, current | `live-p4/reobserve/B-xw*.json` |
| codex | reviewer | re-observed, current | `live-p4/reobserve/B-xr*.json` |
| vibe | worker, reviewer, helper, evaluator | refused (by Vibe's design) | `live-p4/vibe/` |

Each re-observed row's launch and configuration identity equalled the stored row (`hook_source 496c1102…`);
each run exercised a permitted read and the secret and alias denials, and, by role, the work-tree write and
edit or their denial. The Claude Code worker's outside-write denial was observed in Task 6b, after the model
declined to attempt it in Task 6.

## What the live gate did prove for Vibe

Recorded as evidence, not as acceptance: model delivery (`mistral-medium-3.5` pinned and delivered on every
call, no fallback, telemetry off, no provider key in the client environment), persisted project trust without
`--trust`, discovery of the run-owned project files, authenticated loopback memory with bearer redaction and
service cleanup (worker), bound resume with tampered-artifact refusal, caps, and per-call usage rows. Two
Magnolia defects were found and fixed: Vibe 3.1.2 `--output json` is one message array (`43c574e`), and the
tool-name mismatch analysis showed no hook-adapter gap.

## Budgets

| Client | Plan 4 live calls | Cumulative |
| --- | --- | --- |
| Vibe | 11 (9 gate, 2 reproduction) | 16 of 30 |
| Claude Code | 5 (re-observation) | 36 of 40 |
| Codex | 2 (re-observation) | 36 of 44 |
| OpenRouter (memory server) | 29 calls, US$0.006 | — |

Vibe reported about US$0.48 of Mistral usage in total. The owner's Vibe trust file and Codex `config.toml`
were restored byte-identical after each pass (`cmp`); owner Magnolia state was unchanged.
