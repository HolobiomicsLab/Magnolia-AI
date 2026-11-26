# Harness acceptance, 15 November 2026

Live acceptance of Magnolia's hook core in Claude Code, Codex and Mistral Vibe
(Plan 2, Task 13). Each check is marked **seen**, **not seen** or **unknown**,
with the evidence that decides it. OpenCode is not installed on the test Mac,
so OpenCode itself was not run; its side of the comparison uses
`tests/fixtures/v2_session_export.json`.

## Set-up

- A demo workspace under `$TMPDIR` (removed afterwards): a copy of the launcher
  inputs, one project `demo` with a goal naming the codename `HELIOTROPE-7`,
  and one memory entry, "Haddock3 workflow recovery after timeout", that
  matches the command `haddock3 run workflow`.
- Each client's project configuration already held a member-owned hook (a
  `PostToolUse` / `post_tool` command appending to a log). No member MCP
  server was added.
- The compchem-tools server ran on a private port of the demo.
- Each run replayed the argv, cwd and env of
  `MAGNOLIA_DRY_RUN=1 magnolia --harness H demo`, plus the non-interactive
  flags:
  - Claude Code: `-p "<task>" --allowedTools mcp__compchem-tools__run_shell`;
  - Codex: `exec`, with `--dangerously-bypass-hook-trust` on the temporary
    project only (the owner's choice; never in a rendered configuration);
  - Vibe: `-p … --trust --auto-approve`, `VIBE_DISABLE_TELEMETRY=true`.
- The task asked the model to state the codename, call `run_shell` with
  `echo haddock3 run workflow`, say whether a past learning was shown, and
  finish with one sentence.

Model calls: Claude Code 1, Codex 2 (the first failed before any model call,
outside a trusted directory, and was rerun after `git init`), Vibe 1 (its
configured model, `zai-glm-5-3-flash`). Critic: 3 calls through OpenRouter,
`deepseek/deepseek-v4.1-flash`. Recording the transcript fixtures earlier
took one more call each for Claude Code and Codex.

## Results

| Check | Claude Code | Codex | Vibe |
|---|---|---|---|
| Boot context (reply names the codename) | seen | seen | seen |
| Action note: `action-retrieval.jsonl` `injected: 1` and a `surfacing.jsonl` row | seen | seen after fix `da06fd0` | seen |
| Session row in `agent-sessions.jsonl` and transcript file | seen | seen | seen |
| Recovered reply text in the transcript | not seen live in `-p`; seen on replay | seen | seen after fix `1633fce` (replay) |
| Built-in shell refused by the policy | not reached: the shell is removed by `--disallowedTools Bash` | seen | not reached: removed by `disabled_tools` |
| Member's own hook still runs | seen | seen | seen |
| Member's own MCP server | not tested | not tested | not tested |
| Skills | linked under `.claude/skills`; listing by the model not checked | none (documented absence) | linked under `.agents/skills`; listing by the model not checked |
| Critic row | seen on replay (verdict row) | not seen: judge reply had no JSON | not seen: judge reply had no JSON |

### Evidence and findings

**Claude Code.** The model named the codename and called `run_shell`; the
retrieval row shows one injected learning and the application probe marked it
applied. The `Stop` hook is asynchronous in the rendered settings, and in `-p`
mode the process exited before it finished: no reply row, no critic. Replaying
the same `Stop` payload by hand recorded the reply and produced a critic
verdict. Open for Plan 3: run the turn-end work synchronously in
non-interactive runs, or detach it from the client process.

**Codex.** Codex reports MCP tools as `mcp__compchem_tools__run_shell`
(underscores in the server name), so the first run found no match. Fix
`da06fd0` restores the hyphens of known servers; the second run injected the
learning and recorded the application. The policy blocked the built-in shell.
The critic call returned HTTP 200 with no JSON object.

**Vibe.** Vibe's journal shows the post-tool hook appending the learning
("Appended 620 chars to tool result"), yet the model answered that no past
learning was shown; what Vibe sent to the model is not observable from its
logs. Vibe's `post_agent` payload carries no `turn_id`, so reply recovery and
the critic did not run. Fix `1633fce` takes the turn id of the same agent's
last recorded prompt; a replay of the real `post_agent` payload then recorded
the three assistant blocks of the turn. The critic call on that replay again
returned no JSON object.

**Critic judge.** Two of three calls returned a reply with no JSON object. The
error rows did not record why. Since `1633fce` they record `finish_reason` and
the reply length, which will show whether the 500-token limit cuts the
verdict off. Open for Plan 3.

**Pre-tool errors.** Claude Code's `ToolSearch` pre-tool events logged
spurious "adapter returned no event" errors; fixed in `e7f113a` (an ignored
event logs only when the policy would have blocked it).

## Distillation

`scan_and_distill` ran once on the demo store with a real memory model
(OpenRouter, `deepseek/deepseek-v4.1-flash`), capped at three model calls by a
wrapper that refused any further HTTP call. It ran in dialogue mode over the
hook transcripts.

| Session | Calls | Outcome |
|---|---|---|
| Claude Code | 1 | seen: two entries staged, both with `observed_in_sessions` naming the Claude session |
| Codex (two sessions) | 2 | seen: the model returned no candidate for either session; cursors written |
| Vibe | 0 | not run: the cap was reached first (the fourth call was refused) |

One of the two Claude entries, "HADDOCK3 workflow timeout recovery", restates
the learning Magnolia itself had injected into that session. Distillation can
therefore re-learn its own action notes; consolidation is expected to merge
such a duplicate, but this run did not check it. Open for Plan 3: exclude
injected notes from the distilled dialogue, or mark them in the transcript.

The OpenCode side of the comparison rests on the test suite, which distils
`tests/fixtures/v2_session_export.json` through the same pipeline; OpenCode
was not run live.

## Trust boundaries

Recorded after Tao's review of PR #27 (18 November 2026).

- **The client's transcript reference is trusted, not confined.** The hook
  reads the transcript path the client puts in its hook payload: Claude Code's
  `transcript_path` (or `agent_transcript_path` for a sub-agent), and the
  session directory Vibe reports. `hook/events.py` takes it as given, and
  `hook/client_transcripts.py` reads it when it is a regular file (Claude Code,
  Codex) or a directory (Vibe). It checks no root. The readers are narrow: a
  bounded tail, the client's own row shapes, scrubbed output. Even so, a
  crafted payload could point the hook at a JSONL file of the expected shape
  elsewhere. Its assistant blocks would then be appended to the session
  transcript and memory. In normal use only the client writes the hook's input;
  the model has no channel to it. A process that can run the hook command with
  its own input can pass any path. That includes a model with a shell tool. The
  shipped template turns the shell off (`"tools": {"bash": false}`), and a
  member who turns it back on extends this trust to the model. Planned: confine
  the path to each client's transcript root and reject symlinks that resolve
  outside it. This change touches the hook, so it moves the capability rows'
  `hook_source`. It will land with the next live re-observation of those rows.
- **Claude Code permissions add up across sources.** Magnolia passes its
  settings with `--settings` and loads the member's project and local settings
  with `--setting-sources project,local`. Claude Code combines list keys such
  as `permissions.allow` across settings sources instead of replacing them, so
  a member's project `allow` entries still apply next to Magnolia's two MCP
  entries. Project `allow` rules still wait for the member to trust the folder;
  `deny` and `ask` apply at once (Claude Code settings documentation, "Lists
  merge instead of overriding"). The rendered `settings.json` is Magnolia's own
  file in the harness folder. If its `permissions.allow` is not a list, which
  Claude Code would not accept, the render replaces it with Magnolia's list.
- **The member's user settings are not loaded.** `--setting-sources
  project,local` leaves out `~/.claude/settings.json`, so the member's global
  permissions and hooks do not apply in a Magnolia launch. This is deliberate:
  the launch behaves the same for every member. The member's login and model
  choice are unaffected. The Claude Code documentation does not say whether
  this also drops the MCP servers in `~/.claude.json`. Magnolia passes
  `--strict-mcp-config`, so only its own `mcp.json` is used either way.
- **The pre-tool policy is a guard, not a sandbox.** `hook/policy.py` blocks a
  denied tool before it runs, and fails closed when the policy file cannot be
  read. Its docstring lists the deliberate fail-open paths: stdin that is not a
  JSON object, an unknown harness, no recoverable tool name, a failed adapter
  render, a failed stdout write. Vibe also removes the shell from its tool
  list. Claude Code and Codex rely on the hook plus `--disallowedTools` or the
  Codex sandbox. Confinement that must hold against the model comes from the
  client's own sandbox and the campaign roles of Plan 3, not from this hook
  alone.

## Not covered

- OpenCode was not run (not installed on this Mac).
- No member MCP server was configured, so server coexistence was checked only
  in the rendered configuration and the tests.
- Interactive sessions were not run; every client ran non-interactively.
