# Harness probes, 15 Nov 2026 (Task 0 of the harness-neutral plan, P0)

Fixtures: `mcp-servers/compchem-memory/tests/fixtures/hooks/<client>/<scenario>/NN-<event>[.async][.sub].json`
(arrival order; `.async` = from an `async: true` hook; `.sub` = event carrying `agent_id`).
Scripts: `scripts/probes/{probe_mcp.py,log_hook.py,second_hook.py,run_probes.sh,make_fixtures.py}`.
Fixture paths below are relative to `fixtures/hooks/`.

**The fixtures are scrubbed, not verbatim.** The raw probe logs no longer exist, so the committed fixtures cannot be
regenerated; they carry the changes of the scrubber version that produced them, plus one hand redaction:
- home and temp paths became `/HOME` and `/TMPDIR/` (and `-TMPDIR-` inside Claude's project directory names); session and
  thread ids under the keys `session_id`, `thread_id`, `sessionId` became `ses-fixture-N` (other thread, turn and item
  UUIDs are as captured); e-mail addresses became `<email>`; `credential_expires_at` and `apiKeySource` were dropped;
- **`token` was rewritten to `tok` everywhere, in field names and values** (Claude's `modelUsage` sub-fields read
  `inputtoks`, `outputtoks`, `cacheReadInputtoks`, `cacheCreationInputtoks`, `thinkingtoks`, `maxOutputtoks`, and a
  `tok_counts` key; free text reads "tok" for "token"), and every `@` became `(at)`. Read these back as `token` and `@`;
- **`usage` and `modelUsage` were replaced by the string `"<dropped>"`**, so no fixture holds a token-usage shape: the
  `turn.completed` usage object of Codex and the result line's `modelUsage` of Claude must be re-captured before Plan 3
  relies on them. The `input_tokens` figures in the Codex context-size table below were read from the raw logs and have
  no fixture;
- by hand (15 Nov), the output of the Codex probe agent's built-in shell calls (`codex/parent/04`, `06`,
  `codex/two-hooks/01`, `02`, `codex/parent-events/08`, `10`) and the file paths in those commands (`codex/parent/03`,
  `05`, `codex/parent-events/07`, `09`) were replaced by `<redacted: owner's local agent configuration>`: the agent read
  the owner's own instruction files at start. Keys and event order are unchanged.

`scripts/probes/make_fixtures.py` now keeps field names and the `usage` numbers, elides built-in shell output
(`<shell output elided, N chars>`), replaces home paths outside `~/.codex` and `~/.claude` with `/HOME/<elided>`, and
redacts only the values of secret-named keys and Bearer/`sk-` strings. Its docstring lists every change. The plan's
Step 5 grep (`token|bearer|@|/Users/`) could be satisfied by the renaming above; it is replaced by
`make_fixtures.py --check <fixtures_root>`, which looks for the home directory, the login and full name, e-mail
addresses, `/Users/` paths and Bearer/`sk-` strings, and exits 1 on any hit.

Status: Claude Code and Codex probed (Codex hooks via `--dangerously-bypass-hook-trust` on temp projects at the owner's request, 15 Nov). Vibe 3.1.2 probed (run by the lead on the owner's go, with `--trust` on temp projects only).
Anything not observed is "unknown", not "absent".

## Executables

| Client | Version | Path | sha256 |
|---|---|---|---|
| Claude Code | 2.1.325 | `~/.nvm/versions/node/v24.16.0/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` | `84590fefe6338be9087f5814b61f9a54c5f21524163a4c127cb5a808e31009f2` |
| Codex | codex-cli 0.154.0 | `~/.codex/packages/standalone/releases/0.154.0-aarch64-apple-darwin/bin/codex` | `4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc` |
| Vibe | 3.1.2 (`uv tool install mistral-vibe==3.1.2`) | `~/.local/share/uv/tools/mistral-vibe/bin/vibe` (symlinked from `~/.local/bin/vibe`) | `c3190bb894f335e32030046e07286daad17a172c35d303e108a448fcd57ca5e9` |

## Commands (replayable)

All runs: `scripts/probes/run_probes.sh <function> [args] </dev/null` (stdin redirected, otherwise Claude waits 3 s on stdin).
Each run builds its own temp dir under `$TMPDIR` (`probe-claude-*`, `probe-codex-*`), writes `settings.json`/`mcp.json` (Claude) or
`.codex/config.toml` + `.codex/hooks.json` (Codex) there, and logs `{argv, stdin}` to `$PROBE_LOG`. No global config is edited; no `--bare`.

- `claude_parent_sub`: `claude -p "Call the echo tool with text 'top'. Then ask a subagent to call echo with text 'sub'." --settings S --setting-sources user --mcp-config M --strict-mcp-config --output-format stream-json --verbose --allowedTools mcp__probe__echo Task --model haiku`
- `claude_tool_error`: same flags, prompt "Call the echo tool with text 'fail' and report the outcome in five words."
- `claude_context small|max`: default model, SessionStart hook prints `additionalContext` read from `$PROBE_INJECT`; prompt "Reply with the last token of your session context." (small: 2 000 B; max: 256 000 `B` + `\n\n[truncated at 256k chars]\n` + `MAGNOLIA-END-7Q` = 256 043 B).
- `codex_run <name> <prompt> [-m MODEL]`: `codex exec --json -C <tmp> -s workspace-write [-m MODEL] <prompt>` in a `git init`'d temp project with `.codex/config.toml` (`[mcp_servers.probe]`) and `.codex/hooks.json`.

Hooks registered (Claude): SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure (added for the second tool-error run), Stop, SubagentStart, SubagentStop, SessionEnd, all running `log_hook.py claude`; PostToolUse additionally has `second_hook.py` (sync) and `log_hook.py claude-async` after `sleep 2` with `async: true`.

## Claude Code 2.1.325

Hooks loaded (first line is SessionStart in every run). The user's own plugin hooks also fire under `--setting-sources user`
(six SessionStart hooks ran, ours among them); probes filter on our own log.

| Event | Fields seen (beyond `session_id`, `transcript_path`, `cwd`, `scratchpad_dir`, `hook_event_name`, `prompt_id`) | Fixture |
|---|---|---|
| SessionStart | `source` ("startup") | `claude/parent/01-SessionStart.json` |
| UserPromptSubmit | `prompt`, `permission_mode` | `parent/02-UserPromptSubmit.json` |
| PreToolUse | `tool_name`, `tool_input`, `tool_use_id`, `effort{level}`, `permission_mode`, `mcp_server` (MCP tools only; `{name, source}`) | `parent/05-PreToolUse.json` |
| PostToolUse | PreToolUse fields + `tool_response`, `duration_ms` | `parent/06-PostToolUse.json` |
| PostToolUseFailure | PreToolUse fields + `error` (string), `is_interrupt`, `duration_ms` | `tool-error/06-PostToolUseFailure.json` |
| SubagentStart | `agent_id`, `agent_type` | `subagent/01-SubagentStart.sub.json` |
| SubagentStop | `agent_id`, `agent_type`, `agent_transcript_path`, `stop_hook_active`, `background_tasks`, `session_crons`, `permission_mode`, `effort` | `parent/22-SubagentStop.sub.json` |
| Stop | `stop_hook_active`, `last_assistant_message`, `background_tasks`, `session_crons`, `permission_mode`, `effort` | `parent/17-Stop.json` |
| SessionEnd | `reason` ("other" in print mode) | `parent/26-SessionEnd.json` |

Answers:
- **Tool name form**: MCP tools are `mcp__<server>__<tool>` (`mcp__probe__echo`), plus a separate `mcp_server` object. The subagent tool is named **`Agent`** (not `Task`; `Task` in `--allowedTools` still worked). Deferred-tool loading shows up as `ToolSearch` events, and the subagent's final hand-back as `SubagentHandback` events: adapters must ignore both.
- **`agent_id` in subagent**: yes. Every tool event inside the subagent carries `agent_id` and `agent_type` ("general-purpose"); parent events carry neither (`claude/subagent/`, compare `parent/05` and `parent/12`). PostToolUse for the `Agent` tool itself is a parent event (no `agent_id`) and its `tool_response` has `agentId`, `isAsync: true`.
- **Order**: the subagent ran in the background (`isAsync`). Parent order: Pre/Post Agent, SubagentStart, subagent tool events interleaved with parent tool events, parent **Stop (17) before the subagent finished**, then a second UserPromptSubmit (21, `prompt` = an `<agent-message>` hand-back), SubagentStop (22), second Stop (25), SessionEnd (26). So Stop can fire twice per `-p` run and is not "end of work".
- **Two PostToolUse hooks**: both ran for every tool call (six `second-hook` lines for six PostToolUse events, `parent` run; log2). Order across separate hook commands was not shown as deterministic: the sync commands ran as the event fired; the async one logged later and out of order (`07`, `11`, `14`, `18`, `23`, `24` tagged `.async`). `async: true` hooks do not block and may arrive after later events, even after Stop.
- **Stop payload**: see table. **Result line** (stream-json `type: result`): `subtype: success`, `result` text, `num_turns`, `terminal_reason`, `permission_denials: []`, `subagent_stats`, `modelUsage` (replaced by `"<dropped>"` in the fixture), `origin` (`parent/27-result-stream-line.json`). Observed: with a backgrounded subagent the result line came from the task-notification turn (`origin.kind: task-notification`).
- **Tool error**: PostToolUse does **not** fire for a failing MCP tool; `PostToolUseFailure` does (`tool-error/06-PostToolUseFailure.json`). First run, without that hook registered, showed no event at all after PreToolUse: an adapter must subscribe to PostToolUseFailure, or failures are invisible. The model reports "Error calling tool 'echo': probe failure requested".
- **Context-size** (default model; `claude/context-small/`, `claude/context-max/`):

| | bytes emitted by hook | hook output accepted by client | model reported marker |
|---|---|---|---|
| control | 2 000 (stdout 2 084 with JSON wrapper) | yes (exit 0, outcome success) | **yes**: `MAGNOLIA-END-7Q` |
| max | 256 043 (stdout 256 129) | yes (exit 0, outcome success, no error) | **no**: model said its last context token was the date line, and that only a ~2 KB preview of the hook output was in context with the rest "saved to disk" (model's own account, not verified; I did not look for the file) |

  Consequence for Plan 2: **a 256 KB SessionStart `additionalContext` is accepted but not delivered intact**, so the boot payload must not rely on the hook for anything past about 2 KB (preview size inferred from one model self-report; unknown whether the threshold is 2 KB, 10 KB or other). Use a short pointer in the hook plus `--append-system-prompt-file` or a file the agent reads. Intermediate sizes were not probed.

Not observed (unknown): PreToolUse `deny` in Claude (not required by the brief); behaviour of hooks outside `-p` mode; `PreCompact`/`Notification` events.

## Codex 0.154.0

Project hooks do not load by default: with only `.codex/hooks.json` in a temp project, `codex exec` ran the MCP tool but wrote no hook log and
printed nothing about hooks (hooks need persisted hook trust). On 15 Nov, at the owner's explicit request, the lead re-ran the probes with
`--dangerously-bypass-hook-trust` **on throwaway `$TMPDIR/probe-codex-*` projects only** (our own logging hooks and the probe MCP server, never any
other directory or config file). The agent that wrote this report ran no Codex call in this round; it processed the lead's logs.
All runs: `codex exec --json -C <tmp> -s workspace-write --dangerously-bypass-hook-trust -m gpt-5.6-luna "<prompt>"`, stdin closed
(`-m gpt-5.6-luna` because the default model `gpt-6-sol` is refused by the account, `codex/turn-failed/`). Codex prints the bypass warning as two `item.type: error` items.
Hooks registered: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, plus a second independent PostToolUse command.

| Event | Fields seen | Fixture |
|---|---|---|
| SessionStart | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, `permission_mode`, `source` | `codex/parent/01-SessionStart.json` |
| UserPromptSubmit | + `prompt`, `turn_id` | `codex/parent/02-UserPromptSubmit.json` |
| PreToolUse | + `tool_name`, `tool_input`, `tool_use_id`, `turn_id` | `codex/parent/03-PreToolUse.json` |
| PostToolUse | PreToolUse fields + `tool_response` (no `duration_ms`) | `codex/parent/04-PostToolUse.json` |
| Stop | `last_assistant_message`, `stop_hook_active`, `turn_id`, `model`, `permission_mode` | last file of `codex/parent/` |
| SubagentStart / SubagentStop / SessionEnd / PostToolUseFailure | not registered, not observed: unknown | |

Answers:
- **Tool name form**: `Bash` (built-in shell; `tool_input.command`), `mcp__probe__echo` (same `mcp__<server>__<tool>` form as Claude, but no separate `mcp_server` field), `spawn_agent`, `multi_agent_v1wait_agent`. In the `--json` stream the same MCP call is `item.type: mcp_tool_call` with separate `server`/`tool` fields (`codex/parent-events/`).
- **`agent_id` in a subagent** (`codex/subagent/`, spawn_agent): UserPromptSubmit, PreToolUse and PostToolUse inside the subagent carry `agent_id` (its thread id; also in the `spawn_agent` `tool_response` as `agent_id` plus `nickname`) and `agent_type` ("default"); parent events carry neither. No SubagentStart/Stop hook was registered, so unknown. The subagent's own Stop was not seen in the log (16 hook lines in total): unknown whether it fires.
- **Two PostToolUse hooks** (`codex/two-hooks/`): both ran; the second hook wrote six lines for six PostToolUse events. Relative order not tested.
- **Stop**: carries `last_assistant_message` and `stop_hook_active`; one Stop in the run.
- **Tool failure**: in the `--json` stream (no hooks) a failing MCP tool is `status: "failed"`, `error: null`, text in `result.content[0].text`, then `turn.completed` (`codex/tool-error/`). Hook-side failure event: unknown.
- **Model refused**: `error` then `turn.failed` (`codex/turn-failed/`). Usage fields are replaced by `"<dropped>"` in the fixtures (see the top of this report).

Denial (`-s workspace-write`; the `--json` stream contains no `command_execution` item for either probe, so the mechanism is inferred from the hook log, the agent's reply and the file system):
- **(a) `deny-shell`** (`PROBE_DENY=Bash`, `codex/deny-shell/`): PreToolUse(Bash, `echo hi > shell-ran.txt`) logged, hook printed `permissionDecision: "deny"`; **no PostToolUse followed**, the agent replied "Security hook blocked the shell command; no file was created", and the file did not exist. Mechanism: **hook**. The denial held.
- **(b) `deny-write`** (no deny hook, `codex/deny-write/`): command `mkdir -p /private/var/tmp/magnolia-probe-deny && echo hi > .../x.txt`; PreToolUse and PostToolUse both fired (no hook decision), the agent reported "permission denied", and neither the directory nor the file existed afterwards. Mechanism: **sandbox**. The earlier probe (before hooks loaded) showing that a write under `$TMPDIR` is allowed in `workspace-write` (`$TMPDIR` is a writable root) is from console output; its run was overwritten and has no fixture.

Context size (`codex/context-small/`, `codex/context-max/`, SessionStart `additionalContext`, same payloads as Claude):

| | bytes emitted | hook output accepted | model reported marker | turn `input_tokens` |
|---|---|---|---|---|
| control | 2 000 | yes | yes (`MAGNOLIA-END-7Q`) | 20 671 |
| max | 256 043 | yes | yes (`MAGNOLIA-END-7Q`) | 22 989 |

Caveat: 256 000 identical letters tokenise very compactly (about 2.3 k extra tokens), so this shows that the end of the payload reached the model, not that every character did, and only a realistic text payload can measure a limit. The same caveat applies to the Claude result, where the model reported seeing only a preview. Codex accepted and delivered the tail of a 256 KB payload; Claude did not.

## Vibe 3.1.2

Setup: per run a temp project under `$TMPDIR` with `.vibe/config.toml` (`[[mcp_servers]] name = "probe", transport = "stdio"`) and `.vibe/hooks.toml`
(`[[hooks]]` entries with `name`, `type`, `command`, optional `match`, `timeout`, `strict`; one hook per type plus a second independent `post_tool`).
Project hooks and project MCP config are read only from a **trusted** folder: the lead passed `--trust` per run, on these temp projects only, on the owner's go (15 Nov);
this report's author ran no Vibe call. Every run: `VIBE_DISABLE_TELEMETRY=true vibe -p "<prompt>" --output json --workdir <tmp> --trust --max-turns 8 --max-price 0.05 --output-dir <tmp>/out </dev/null`
(plus the tool flags named per scenario), default model `zai-glm-5-3-flash` ("GLM-5.3 Flash (Mistral Hosted)", free tier), so every run reports `cost_usd` 0.0 and the price cap was never exercised.
8 model runs; one more attempt failed on argument parsing before any call.

| Hook event | Fields seen (beyond `hook_event_name`, `session_id`, `parent_session_id`, `transcript_path`, `cwd`) | Fixture |
|---|---|---|
| session_start | `source`, `agent_name`, `model` | `vibe/parent/01-session_start.json` |
| pre_agent | `agent_name`, `turn_id`, `prompt` | `vibe/parent/02-pre_agent.json` |
| pre_tool | `tool_name`, `tool_call_id`, `tool_input` | `vibe/parent/03-pre_tool.json` |
| post_tool | + `tool_status` (success/failure), `tool_output`, `tool_output_text`, `tool_error`, `duration_ms` | `vibe/parent/04-post_tool.json` |
| post_agent | `agent_name` | last agent events of `vibe/parent/` |
| notification | `notification_type`, `message`, `agent_name` | `vibe/parent/` |
| session_end | `reason` | last file of `vibe/parent/` |

Answers:
- **Tool name form**: qualified `mcp_<server>.<tool>` (`mcp_probe.echo`), built-ins `file_system.bash`, `agents.spawn`. No `task` tool. Matchers accept short or qualified names, but `--enabled-tools mcp_probe.echo` **hid** the MCP tool from the model (no call; `vibe/enabled-tools-hides-mcp/`): the tool flags do not address MCP tools by that name.
- **Subagents** (`vibe/subagent/`): spawned through `agents.spawn`; `pre_agent`/`post_agent` fire per subagent; events inside the child carry their own `session_id` (`child-...`) and `parent_session_id` (the parent session). Parent events have `parent_session_id: null`. The two subagents here ended with status `failed` in export.json although each completed its echo call (cause not investigated: unknown). `session_start` never fires for subagents, per the docs.
- **Two post_tool hooks** (`vibe/two-hooks/`): both ran; the second hook wrote five lines for five post_tool events.
- **Tool error** (`vibe/tool-error/`): a failing MCP tool arrives as a normal `post_tool` with `tool_status: "failure"`, `tool_error` set and `tool_output: null`; there is no separate failure event.
- **Headless approval** (`vibe/headless-no-approval/`): without `--auto-approve` the MCP tool is denied ("needs approval, which a headless run cannot give") and the run stopped at `turn_limit`; hooks still all fired. Governor runs need `--auto-approve` plus explicit `--disabled-tools`.
- **What a hook returns** (from Vibe's bundled docs, exercised here for the two used): exit 0 and empty stdout = pass; JSON `{"decision": "deny", "reason": ...}` on `pre_tool` denies; `{"hook_specific_output": {"additional_context": ...}}` on `session_start`/`pre_agent`/`post_tool` adds context; `strict = true` (pre_tool, post_tool, pre_agent) escalates a hook failure to a block. Unparseable stdout counts as failure.
- **Context size** (`vibe/context-small/`, `vibe/context-max/`, `session_start` `additional_context`):

| | bytes emitted | hook output accepted | model reported marker | `input_tokens` |
|---|---|---|---|---|
| control | 2 000 | yes | no (the model said it had no hidden token: inconclusive) | 21 503 |
| max | 256 043 | yes | yes (`MAGNOLIA-END-7Q`) | 85 264 |

  The payload reached the model (about 63 k extra input tokens, a far less compact tokenisation than in the other clients). Caveat: repeated letters; only realistic text can measure a limit.
- **Instructions file, skills location, disabling the shell**: the shell is disabled with `--disabled-tools file_system.bash` (also `file_system.write_file`, `file_system.edit`). Per the bundled docs, instructions are `AGENTS.md` (project root, `~/.vibe/AGENTS.md`) and skills are `.vibe/skills/` and `.agents/skills/`; neither was exercised.

`export.json` (written by `--output-dir`, fixtures keep a reduced copy named `NN-export.json`; the `config` block embeds the effective configuration and is dropped; session journal files were not kept):
`outcome` (`finished` | `turn_limit`), `stop_reason`, `error`, `exit_code`, `usage{input_tokens, output_tokens, cached_input_tokens, total_tokens}`, `cost_usd`, `steps`, `limits{max_turns, max_price, max_tokens, time_limit_s}`,
`subagents[]` (each with `session_id`, `parent_session_id`, `agent_name`, `status`, `model`, own `usage`, `cost_usd`, `pricing_model`, `pricing_fallback`, `steps`), `started_at`, `ended_at`, `vibe_version`, `schema_version`.
For a usage governor this offers exact per-run and per-subagent tokens and cost, the limit that stopped the run (`outcome: turn_limit`, exit code 3 in the no-approval run) and `--max-turns/--max-price` as hard caps. On the free tier cost is 0.0, so a money cap can only be verified on a paid model: unknown. Parent `usage` for the three-session run was 133 206 input / 659 output tokens (`vibe/parent/`).

Denial (the table below): `deny-shell` (`PROBE_DENY=bash`, `--auto-approve`, write/edit disabled, `vibe/deny-shell/`): pre_tool `file_system.bash` denied by the hook, no post_tool, no file created. `deny-write` (`vibe/deny-write/`): no deny hook; the shell wrote `/private/var/tmp/magnolia-probe-deny-vibe/x.txt` outside the project, `$TMPDIR` and `/tmp` with exit 0; the lead deleted it afterwards. Vibe has no sandbox: nothing but a hook or the tool list stops an out-of-project write.

## Usage-limit texts (Step 4b)

No limit was hit. Not recorded from documentation either: no client help text or CLI documentation consulted contained the limit message. Unknown.

## Built-in tools that can be denied, by mechanism

| Client | Tool | Mechanism | Denial seen holding? |
|---|---|---|---|
| Claude Code | MCP tool | PreToolUse hook `permissionDecision` / `--allowedTools` | not probed |
| Codex | built-in shell (`Bash`) | PreToolUse hook `deny` | yes, hook (`codex/deny-shell/`) |
| Codex | write outside project, outside `$TMPDIR` and `/tmp` | sandbox `workspace-write` | yes, sandbox (`codex/deny-write/`) |
| Vibe | shell (`file_system.bash`) | `pre_tool` hook deny, or `--disabled-tools file_system.bash` | hook deny: yes (`vibe/deny-shell/`) |
| Vibe | write outside the project | none (no sandbox) | no: the write succeeded (`vibe/deny-write/`); only a hook or disabled tools can stop it |

## Consequences for design sections 5 and 7

- Section 5 (hook core): normalise Claude `mcp__server__tool` and Codex `{server, tool}` separately; treat `agent_id` as the subagent discriminator on Claude; ignore `ToolSearch`/`SubagentHandback`; subscribe to PostToolUseFailure; accept multiple Stop and UserPromptSubmit events per run; never assume async hook ordering.
- Section 7 (boot context): do not ship the boot payload through SessionStart `additionalContext` beyond a short pointer; the 256 KB maximum was accepted by Claude but the model saw about 2 KB. Codex delivered the tail of a 256 KB payload (marker seen), with the tokenisation caveat above.
- Vibe: no sandbox, so a Vibe worker must be confined by a `strict` `pre_tool` hook on `re:bash|process_start` plus `--disabled-tools` for write/edit; MCP tools need `--auto-approve` in headless mode and are not hidden or selected by `--enabled-tools mcp_...`; `export.json` is the usage-governor source.
- Section 8.1 (Codex worker denial): verified for the built-in shell (hook deny holds, no PostToolUse) and for writes outside the writable roots (sandbox). Hooks must be trusted for the campaign worker project; the bypass flag is not to be baked into any config.

## Calls spent

Claude: 5 headless calls (parent 1, tool-error 2, context small 1, context max 1; two earlier invocations aborted on a script bug before launching claude). Codex: 10 by the agent and the lead together, plus one run that hung on stdin with no model call. Earlier round (agent): 5 (MCP probe 1, tool-error 1, deny-write 3 of which 2 refused by the model setting). This round (lead, with the bypass flag): 5 (parent+subagent+two-hooks, deny-shell, deny-write, context-small, context-max).

Vibe: 8 model runs by the lead (parent, tool-error, deny-shell, deny-write, context small and max, plus a no-approval and an enabled-tools variant of the parent probe); total cost reported by export.json 0.0 USD (free default model).
