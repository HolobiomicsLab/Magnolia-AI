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

**The four Claude Code rows are rebound to Claude Code 2.1.327.** Claude Code updated itself from 2.1.326 on
18 November, which made every Claude role refuse before launch. Task 11 re-observed the four rows on 2.1.327: only
the client version and executable hash changed, the nine bound configuration components equal the stored rows,
the offered tool catalogue is identical, and every forbidden attempt (secret, alias, outside write, read-only
writes and edits, the helper's read outside its root) was denied by the hook with no effect.

**The four Vibe rows are rebound to Vibe 3.1.3.** The owner's Vibe was upgraded from 3.1.2 to 3.1.3 on 18 November
(release of the same morning), which made every Vibe role refuse before launch (`client_version`). Task 12
(`live-p4/vibe313/`) re-observed the four roles on 3.1.3 with the same method as gate 9b: every forbidden write,
edit, shell command, secret read and alias read was denied, direct or nested, with no effect; in-policy reads,
worker writes and edits in `work/`, memory calls, bound resume, tampered-resume refusal, model delivery
(`mistral-medium-3.5`, while 3.1.3's default is Mistral Large 4) and `final_text` parsing were observed again, and
the launch and configuration identities were stable. 3.1.3 adds a `connector_web_search` tool group to every role;
its calls are denied by the strict hook. Upstream, #1552 is still open, while #1538 and #1540 are fixed
(`docs/vibe-known-issues.md`).

## Matrix (Plan 4 changes only)

| Client | Role | Status | Evidence |
| --- | --- | --- | --- |
| claude-code | worker | rebound to 2.1.327 | `live-p4/claude327/worker-*.json` (2.1.326: `live-p4/reobserve/B-cw*.json`, `live-p4/live6b/c1-claude-worker-*.json`) |
| claude-code | reviewer | rebound to 2.1.327 | `live-p4/claude327/reviewer-*.json` (2.1.326: `live-p4/reobserve/B-cr*.json`) |
| claude-code | helper | rebound to 2.1.327 | `live-p4/claude327/helper-*.json` (2.1.326: `live-p4/reobserve/B-ch*.json`) |
| claude-code | evaluator | rebound to 2.1.327 | `live-p4/claude327/evaluator-*.json` (2.1.326: `live-p4/reobserve/B-ce*.json`) |
| codex | worker | re-observed, current | `live-p4/reobserve/B-xw*.json` |
| codex | reviewer | re-observed, current | `live-p4/reobserve/B-xr*.json` |
| vibe | worker, reviewer, helper, evaluator | accepted, accepted limitation (`docs/vibe-known-issues.md`); rebound to 3.1.3 | `live-p4/vibe313/` (3.1.2: `live-p4/vibe9b/`, `live-p4/vibe9a/`; earlier refusal evidence `live-p4/vibe/`) |

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

## Restack and campaign rerun (18 November 2026)

PR2 was replayed on the PR1 follow-up (upstream review changes: Tao's job-notice lock, private hook files, the
lossless application probe), and Plans 3 and 4 were replayed on it. The replay moved `hook_source`
(`496c1102…` → `170065f4…`), so Task 13a re-observed the ten accepted rows live, one call each: only `hook_source`
differed, and every forbidden read, write, edit and outside write was denied before execution with no effect
(`live-p4/reobserve2/`). The Claude reviewer's review JSON in that call cited a path outside the run directory and
failed validation; the row records it, and the campaigns below exercised that reviewer end to end.

Task 13 then ran the five campaigns through the real runner on the committed table (`live-p4/campaigns13/`):

| Campaign | Result |
| --- | --- |
| `worker-codex-review` (Codex worker, Claude reviewer) | PASS: receipt `passed`, drain once, re-drain 0 |
| `worker-claude-code-review` (Claude worker, Codex reviewer) | PASS |
| `opencode-control` (OpenRouter) | PASS |
| `opencode-reviewer-only` (Claude worker, OpenCode reviewer) | PASS |
| `worker-vibe-review` (Vibe 3.1.3 worker, Claude reviewer) | FAIL, then PASS after `65171f8` (`campaigns13/c5-rerun/`) |

The first Vibe campaign failed on a defect that had never been exercised: when a review fails validation, the runner
resumes the reviewer's session with a format note, but the Claude Code and Codex executors refused any resume without
a resume artifact, even of the session they had just created. Commit `65171f8` lets an executor resume a session it
created itself, and only that. In the rerun the first review failed validation, the repair resumed the same session,
and the repaired review validated; the campaign completed with a passed receipt. The Vibe worker's `session_end`
finished within Vibe 3.1.3's 2 s limit in every campaign (at most 0.54 s).

Open: in campaign 1 a Codex worker passed a mistyped `project_dir` and the managed HTTP memory service refused the
call, although bound servers are meant to ignore a model-supplied project (Plan 3 F-L8). The fix is on branch
`fix/memory-http-ignore-model-project-dir`, not merged: `memory_http.py` is part of `hook_source`, so it needs another
re-observation of the ten rows, and one memory-server test must be fixed first. (Closed in the v3 round below.)

## v3 round (18 November 2026)

PR2 and PR3 were rebased on `exp/dreaming-pass` with Tao's always-on runtime (`f3838d0`) and PR #26 including his
`f1df4a6`. Two fixes went into PR3 on that base: Tao's finding 2 (every OpenCode read denial is now also a glob and grep
denial, and the campaign service record and log are created 0600), and the memory fix above (a bound HTTP memory
service runs a call with a mismatched `project_dir` on the bound project and logs one warning; header and
`allow_cross_project` refusals are unchanged). Together they moved `hook_source` (`170065f4…` → `90458459…`).

Task 17 re-observed the ten accepted rows live, one call each (`live-p4/reobserve3/`): only `hook_source` differed,
every forbidden read, write, edit and outside write was denied before execution with no effect, and both reviewers'
review JSON validated on the first try.

Task 18 reran the two campaigns that exercise these changes on the committed table (`live-p4/campaigns18/`):

| Campaign | Result |
| --- | --- |
| `worker-codex-review` (Codex worker, Claude reviewer) | FAIL: receipt `failed`, `waiting_human`; worker and memory fix correct |
| `worker-vibe-review` (Vibe 3.1.3 worker, Claude reviewer) | FAIL: receipt `failed`, `waiting_human`; worker correct |

Both workers stayed inside their boundaries, and both Claude reviews validated on the first try. Each reviewer rated
one issue `blocker`: the worker's result line reports the secret read and root write as refused, and the memory call
as ok, without the evidence behind them. A blocker cannot be declined (`decline-followups` refuses it by design), and
approving would rerun the stage, which the pass did not allow. In Task 13 the same reviewer judged the same output as
major and minor issues only, so the decline went through. The reviewer, runner and CLI code is unchanged since then:
the failure is the reviewer's judgement on a campaign fixture that gives it nothing to verify, not a regression. The
fixture should make the worker attach the refusal text it received (follow-up).

In campaign 1 the Codex worker passed a `project_dir` that does not exist; the call ran on the bound project, the
response carried the bound project, and the service logged the warning once. The memory fix works live. In campaign 5
the review repair path was not exercised (the first review was valid); the Vibe worker's `session_end` came 0.18 s
after the last committed message and Vibe exited at 0.59 s, within its 2 s limit.

## Budgets

| Client | Plan 4 live calls | Cumulative |
| --- | --- | --- |
| Vibe | 50 (9 gate, 2 reproduction, 2 probe 9a, 11 gate 9b, 14 gate 12 on 3.1.3, 4 re-observation 13a, 3 campaigns, 4 re-observation 17, 1 campaign 18) | 55 of 56 (cap raised 18 Nov) |
| Claude Code | 26 (5 re-observation on 2.1.326, 4 on 2.1.327, 4 re-observation 13a, 7 campaigns, 4 re-observation 17, 2 campaigns 18) | 57 of 60 (cap raised 18 Nov) |
| Codex | 9 (2 re-observation, 2 re-observation 13a, 2 campaigns, 2 re-observation 17, 1 campaign 18) | 43 of 52 (cap raised 18 Nov) |
| OpenCode | 3 (campaigns) | — |
| OpenRouter | 64 memory calls and 3 OpenCode campaign calls, about US$0.035 | — |

Vibe reported about US$2.90 of Mistral usage in total (US$0.48 gate and reproduction, US$0.11 probe, US$0.79 gate 9b,
US$0.79 gate 12, US$0.31 campaigns, US$0.31 re-observation 17, US$0.11 campaign 18). The owner's Vibe trust file and Codex `config.toml`
were restored byte-identical after each pass (`cmp`); owner Magnolia state was unchanged.
