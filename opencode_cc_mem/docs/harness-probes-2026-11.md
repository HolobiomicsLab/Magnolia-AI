# Harness probes, 15 Nov 2026 (Task 0 of the harness-neutral plan, P0)

Fixtures: `mcp-servers/compchem-memory/tests/fixtures/hooks/<client>/<scenario>/NN-<event>[.async][.sub].json`
(arrival order; `.async` = from an `async: true` hook; `.sub` = event carrying `agent_id`).
Scripts: `scripts/probes/{probe_mcp.py,log_hook.py,second_hook.py,run_probes.sh,make_fixtures.py}`.
Fixture paths below are relative to `fixtures/hooks/`.

Status: Claude Code fully probed. Codex: MCP and event stream probed, **hooks NOT probed** (project hooks need
persisted hook trust; blocked, see below). Vibe: not probed (awaiting the owner's go to install mistral-vibe).
Anything not observed is "unknown", not "absent".

## Executables

| Client | Version | Path | sha256 |
|---|---|---|---|
| Claude Code | 2.1.325 | `~/.nvm/versions/node/v24.16.0/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` | `84590fefe6338be9087f5814b61f9a54c5f21524163a4c127cb5a808e31009f2` |
| Codex | codex-cli 0.154.0 | `~/.codex/packages/standalone/releases/0.154.0-aarch64-apple-darwin/bin/codex` | `4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc` |
| Vibe | not installed | | |

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
- **Stop payload**: see table. **Result line** (stream-json `type: result`): `subtype: success`, `result` text, `num_turns`, `terminal_reason`, `permission_denials: []`, `subagent_stats`, `modelUsage` (dropped from the fixture), `origin` (`parent/27-result-stream-line.json`). Observed: with a backgrounded subagent the result line came from the task-notification turn (`origin.kind: task-notification`).
- **Tool error**: PostToolUse does **not** fire for a failing MCP tool; `PostToolUseFailure` does (`tool-error/06-PostToolUseFailure.json`). First run, without that hook registered, showed no event at all after PreToolUse: an adapter must subscribe to PostToolUseFailure, or failures are invisible. The model reports "Error calling tool 'echo': probe failure requested".
- **Context-size** (default model; `claude/context-small/`, `claude/context-max/`):

| | bytes emitted by hook | hook output accepted by client | model reported marker |
|---|---|---|---|
| control | 2 000 (stdout 2 084 with JSON wrapper) | yes (exit 0, outcome success) | **yes**: `MAGNOLIA-END-7Q` |
| max | 256 043 (stdout 256 129) | yes (exit 0, outcome success, no error) | **no**: model said its last context token was the date line, and that only a ~2 KB preview of the hook output was in context with the rest "saved to disk" (model's own account, not verified; I did not look for the file) |

  Consequence for Plan 2: **a 256 KB SessionStart `additionalContext` is accepted but not delivered intact**, so the boot payload must not rely on the hook for anything past about 2 KB (preview size inferred from one model self-report; unknown whether the threshold is 2 KB, 10 KB or other). Use a short pointer in the hook plus `--append-system-prompt-file` or a file the agent reads. Intermediate sizes were not probed.

Not observed (unknown): PreToolUse `deny` in Claude (not required by the brief); behaviour of hooks outside `-p` mode; `PreCompact`/`Notification` events.

## Codex 0.154.0

Hook part BLOCKED. A project `.codex/config.toml` with `[mcp_servers.probe]` loaded and worked without trust prompts, but the project
`.codex/hooks.json` did not load: `log.jsonl` was never created and `codex exec` printed nothing about hooks. `codex exec --help` documents
`--dangerously-bypass-hook-trust` ("Run enabled hooks without requiring persisted hook trust for this invocation. DANGEROUS."); not used.
The owner must review/trust the hooks once (see the task report for the proposed command). Until then, no Codex hook payload,
no `agent_id` finding, no denial-by-hook finding.

Observed without hooks (event streams, `codex/<scenario>/`):

| Item | Finding | Fixture |
|---|---|---|
| MCP tool call | `item.started`/`item.completed` with `item.type: mcp_tool_call`, `server: "probe"`, `tool: "echo"` (separate fields, not `mcp__probe__echo`), `arguments`, `result{content, structured_content}`, `status` | `codex/parent/05-*`, `06-*` |
| MCP tool failure | `status: "failed"`, `error: null`, error text inside `result.content[0].text`; the turn still ends `turn.completed` | `codex/tool-error/` |
| Shell | `item.type: command_execution`, `command: "/bin/zsh -lc '...'"`, `exit_code`, `aggregated_output` | `codex/deny-write/` |
| Model refused | `{"type":"error"}` then `turn.failed` with `error.message` = JSON string (status 400, `model_not_found`) | `codex/turn-failed/` |
| Usage | `turn.completed.usage{input_tokens, cached_input_tokens, ...}` (dropped in the fixtures) | |
| Noise | `item.type: error` items carry non-fatal warnings (skills budget, model metadata) | |

Environment surprise: the owner's `~/.codex/config.toml` default model changed to `gpt-6-sol` during the session (file mtime 11:44) and the
ChatGPT account refuses it (`codex/turn-failed/`); two runs were wasted. Runs after that pass `-m gpt-5.6-luna` per invocation (config untouched).

Sandbox baseline (`deny-write`, `-s workspace-write`, no hook): a write to `$TMPDIR/probe-outside/x.txt`, outside the project, **succeeded**
(exit 0) because `$TMPDIR` is a default writable root of `workspace-write` on macOS. So that target cannot separate hook from sandbox; a
sandbox-refusal probe needs a user-writable path outside `$TMPDIR` and `/tmp`, and the only candidates are under the home directory or a repo (forbidden here). Unknown.

## Vibe

Not probed (awaiting the owner's go to install mistral-vibe).

## Usage-limit texts (Step 4b)

No limit was hit. Not recorded from documentation either: no client help text or CLI documentation consulted contained the limit message. Unknown.

## Built-in tools that can be denied, by mechanism

| Client | Tool | Mechanism | Denial seen holding? |
|---|---|---|---|
| Claude Code | MCP tool | PreToolUse hook `permissionDecision` / `--allowedTools` | not probed |
| Codex | built-in shell | PreToolUse hook deny (design 8.1) | not probed (hooks not loaded) |
| Codex | write outside project | sandbox `workspace-write` | no: allowed under `$TMPDIR` (writable root); sandbox refusal not observed |
| Vibe | shell | config | not probed |

## Consequences for design sections 5 and 7

- Section 5 (hook core): normalise Claude `mcp__server__tool` and Codex `{server, tool}` separately; treat `agent_id` as the subagent discriminator on Claude; ignore `ToolSearch`/`SubagentHandback`; subscribe to PostToolUseFailure; accept multiple Stop and UserPromptSubmit events per run; never assume async hook ordering.
- Section 7 (boot context): do not ship the boot payload through SessionStart `additionalContext` beyond a short pointer; the 256 KB maximum was accepted by Claude but the model saw about 2 KB. Codex transport unknown until hooks are trusted.
- Section 8.1 (Codex worker denial): unverified; needs the trusted-hook run first.

## Calls spent

Claude: 5 headless calls (parent 1, tool-error 2, context small 1, context max 1; two earlier invocations aborted on a script bug before launching claude). Codex: 5 (MCP probe 1, tool-error 1, deny-write 3 of which 2 were refused by the model setting).
