# Plan 4 (Mistral Vibe) acceptance record — 18 November 2026

Branch `feat/harness-vibe-p4`, plan `docs/harness-neutral-agents-plan-p4-vibe.md`. Offline Tasks 1–5 were
committed `96f7da7..1ba027b`; the live gate ran on a snapshot of `1ba027b`. Evidence is sanitised and lives
under `docs/evidence/live-p4/`. No credential, private prompt, home path or user name is published.

## Outcome

**All four Vibe rows are accepted (18 November 2026), with an accepted limitation.** LFX decided to accept Vibe on
the reading already used for the Codex worker and reviewer: the model-visible catalogue is wider than the role, and
the strict deny-unknown `pre_tool` hook is the enforcement boundary. Every limitation, residual risk and operating
note is listed in `docs/vibe-known-issues.md`.

Why the catalogue is wider: Vibe 3.1.2 cannot remove its filesystem builtins (shell, `write_file`, `edit`) from the
model-visible catalogue. `enabled_tools`, `disabled_tools` and `--disabled-tools` only turn a filtered builtin into a
denial when it is called. The cause is in Vibe's design, reported upstream as mistralai/mistral-vibe#1552:

- `vibe/app_server/_runtime.py:2088` enables the Core filesystem feature unconditionally;
- `vibe/app_server/_runtime.py:3108-3113` maps a filtered tool to execution mode `deny`;
- `mistralai_vibe_local_harness/vibe/_local_actions.py:1659-1669,760-772` sends Core's catalogue to the
  completion call without that filter.

Programmatic tool calling (`run_typescript`, `search_tool_functions`) cannot be disabled and fires no hook
(mistralai/mistral-vibe#1431). Probe 9a (`live-p4/vibe9a/`) showed that calls made from inside a `run_typescript`
program reach the hook and are denied by policy, and that the program sandbox has no filesystem, process or network
access. Gate 9b (`live-p4/vibe9b/`, commit `0f762c9`) then observed all four roles on the corrected render (Vibe's
filter names; subagents and background processes off): every forbidden write, edit, shell command, secret read and
alias read was denied, direct or nested, with no effect; in-policy reads, worker writes and edits in `work/`, and
memory calls were allowed; bound resume, tampered-resume refusal, model delivery, D3 memory with bearer redaction,
and production `final_text` parsing were observed. Launch and configuration identities were equal across four
recomputations per role.

The 17 November ruling that kept Vibe refused is superseded.

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
| vibe | worker, reviewer, helper, evaluator | accepted, accepted limitation (`docs/vibe-known-issues.md`) | `live-p4/vibe9b/`, `live-p4/vibe9a/` (earlier refusal evidence `live-p4/vibe/`) |

Each re-observed row's launch and configuration identity equalled the stored row (`hook_source 496c1102…`);
each run exercised a permitted read and the secret and alias denials, and, by role, the work-tree write and
edit or their denial. The Claude Code worker's outside-write denial was observed in Task 6b, after the model
declined to attempt it in Task 6.

## What the first live gate (Task 6) proved for Vibe

Recorded at the time as evidence, before the acceptance decision: model delivery (`mistral-medium-3.5` pinned and delivered on every
call, no fallback, telemetry off, no provider key in the client environment), persisted project trust without
`--trust`, discovery of the run-owned project files, authenticated loopback memory with bearer redaction and
service cleanup (worker), bound resume with tampered-artifact refusal, caps, and per-call usage rows. Two
Magnolia defects were found and fixed: Vibe 3.1.2 `--output json` is one message array (`43c574e`), and the
tool-name mismatch analysis showed no hook-adapter gap. A third was found later: Magnolia rendered Vibe's core
tool names, which Vibe's filter does not match, so Vibe's own deny-on-call never applied (fixed in `0f762c9`).

## Budgets

| Client | Plan 4 live calls | Cumulative |
| --- | --- | --- |
| Vibe | 24 (9 gate, 2 reproduction, 2 probe 9a, 11 gate 9b) | 29 of 30 |
| Claude Code | 5 (re-observation) | 36 of 40 |
| Codex | 2 (re-observation) | 36 of 44 |
| OpenRouter (memory server) | 29 calls, US$0.006 | — |

Vibe reported about US$1.38 of Mistral usage in total (US$0.48 gate and reproduction, US$0.11 probe, US$0.79 gate 9b). The owner's Vibe trust file and Codex `config.toml`
were restored byte-identical after each pass (`cmp`); owner Magnolia state was unchanged.
