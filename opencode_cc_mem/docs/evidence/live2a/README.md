# Live pass 2a (rerun) evidence: snapshot 497da33

Sanitised. `<G>` is the gate dir, `<OUTSIDE>` the separate outside-credential dir, `~` the home dir,
`<session>` an OpenCode session id. No credential value appears; `<loopback-sentinel>` names the
test-suite placeholder key, not a real key.

| File | Content |
|---|---|
| `dry-runs.json` | Run 1: production and candidate refusals/acceptances, normalized identity per Claude role, OpenCode roles on the production path |
| `capabilities-p3.candidate.json` | Candidate table: production table with `rows[0..3].stale` false (4 leaves) |
| `a5-handoff.json` | Env key names of the managed memory-server spawn and the Claude worker client; OpenCode spawns |
| `spawns.json` | Every non-trivial spawn from the driver process: argv shape, env key names, watched keys, forbidden flags |
| `campaign-routes.json`, `opencode-free-models.json` | Offline route resolution; free model list under gate XDG without auth |
| `run2-opencode-control.json` | Run 2: state, events, worker tool calls, `work/result.txt`, reviewer free-limit error |
| `usage-ledger.json` | Governor ledger rows (gate `XDG_STATE_HOME`) |
| `budget.json`, `nonmodel-launches.json` | Invocation counts |
| `state-checks.json` | Owner state, `~/.codex/config.toml`, `~/.local/share/opencode` before/after |

## Continuation (Claude-only runs, 2026-11-17 09:35 to 09:41 CET)

`<session>` in the `cont-*` files also stands for a Claude session UUID.

| File | Content |
|---|---|
| `cont-runs.json` | Runs W (worker), R (reviewer), H (helper), E (evaluator): pre-launch identity diagnostic vs candidate row, prepared digest, argv, client env keys, result, stream init, tool calls with allow/deny, hook transcript rows; for R the first-try `validate_review` result and a no-model diagnostic |
| `cont-usage-ledger.json` | The four Claude governor ledger rows |
| `cont-spawns.json`, `cont-a5-handoff.json` | Spawn records (env key names only); sentinel reaches the memory server, no provider key in any Claude client |
| `cont-memory-extraction.json` | How extraction against the loopback sentinel failed: `llm-timing` rows, extraction state, distilled markers, service log tail |
| `cont-project-tree.json` | Project file names + sha256 prefixes before and after each run (writes) |
| `cont-state-checks.json` | Owner state, `~/.codex/config.toml`, `~/.local/share/opencode` before/after |
| `cont-version-checks.json`, `cont-nonmodel-launches.json`, `cont-budget.json` | Identity re-checks before each launch, non-model launches, counts |
