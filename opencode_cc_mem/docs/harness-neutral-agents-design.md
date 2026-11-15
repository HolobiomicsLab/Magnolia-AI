# Harness-neutral Magnolia: one core, four agent harnesses

Status: **Draft, design for review** (2026-11-15). Branch `design/harness-neutral-agents`,
on top of `feat/llm-providers-mistral-zai`. Product owner: LFX. Repository owner and
reviewer: Tao. Nothing reaches Magnolia's shared branches without Tao (Collegium D-059).

## 1. Intent

LFX wants Magnolia's agent, and the sub-agents it starts, to run inside the official
agent clients the lab already pays for — **Claude Code**, **OpenAI Codex CLI**, **Mistral
Vibe** — as well as **OpenCode**, each signed in with the member's own subscription.
Magnolia must keep working whichever client runs the agent: project memory, boot
context, retrieval, capture of tool calls, the claim critic, the helper-agent doorbell,
job notices, campaigns and session distillation. Consumption must stay what a person
using their subscription would produce: Magnolia must not turn a subscription into a
substitute for API billing.

Why: the subscriptions are already paid; the strongest models are reachable through
them; and Magnolia's value (memory, verification, scientific tools) should not be tied
to one client.

## 2. Goals, non-goals, anti-goals

Goals
- G1. `magnolia --harness opencode|claude|codex|vibe <project>` opens an interactive
  session with Magnolia's MCP servers, rules, boot context and hooks in that client.
- G2. Campaign workers, campaign reviewers, the helper agents of the agents daemon and
  the router evaluator can run on any of the four clients.
- G3. Each Magnolia behaviour lives once, in Python, behind one normalised event
  contract; each client gets a thin adapter.
- G4. Every non-interactive run on a subscription client goes through a usage governor
  (section 8).
- G5. The OpenCode path keeps working throughout; its tests stay green.

Non-goals
- Sharing one member's subscription with other people or running Magnolia as a hosted
  service on a subscription (Collegium D-004: local clients). A Magnolia checkout belongs
  to one person, who may hold several subscriptions and route different agents and
  tasks to them (section 8.0). Its agents daemon runs helper-agent letters only on that
  person's own logins and refuses letters whose sender project is not under the same
  checkout (*builder check, settled by the author; routing per agent/task: LFX,
  15 Nov 2026*).
- A long-lived hook daemon for latency (possible later; section 11).
- Windows support beyond what Magnolia has today.

Anti-goals (a failure even if it works)
- A1. Magnolia reads, copies, proxies or extracts any client's subscription credentials
  (OAuth tokens, `auth.json`, keychain entries) or calls a vendor API with them. Only the
  vendor's own client talks to the vendor with a subscription login.
- A2. Unbounded autonomous loops: retries on usage limits, parallel fan-out, or
  background agents that keep a subscription saturated.
- A3. A behaviour that silently stops working on one client. Every capability a client
  cannot provide is listed in section 5 with its degraded form.
- A4. Changes that break the OpenCode path or its tests.

## 3. Constraints

- **Vendor terms (checked 2026-11-15).**
  - Claude: Anthropic's help article "Use the Claude Agent SDK with your Claude plan"
    (updated 7 Oct 2026) states that `claude -p` and the Agent SDK draw from the plan's
    subscription limits when signed in with the plan; the separate credit pool announced
    earlier was paused (notice of 15 June 2026). Claude Pro/Max logins inside third-party
    clients such as OpenCode remain disallowed, so Claude runs through Claude Code only.
  - Codex: `codex exec` reuses the saved ChatGPT login and draws on ChatGPT/Codex rate
    limits; `CODEX_API_KEY` would switch to API billing (not used here).
  - Mistral: a Mistral plan covers Vibe, Studio and API usage from one monthly allowance.
  - OpenCode: unchanged (ChatGPT and GLM Coding Plan logins; API keys elsewhere).
- **Collegium.** D-032: Collegium keeps one adapter boundary per harness and treats
  Magnolia as a pinned dependency; D-062: Collegium captures tool calls with its own
  plugin. This design gives Collegium one event contract to adapt to instead of four.
- **Client formats are internal.** Claude Code transcripts, Codex rollouts and OpenCode's
  sqlite schema may change without notice. Magnolia must not depend on them for anything
  it can observe through hooks.
- **Client facts this design relies on** (docs and local `--help`, 2026-11-15; probes in
  phase P0 confirm them):
  - Claude Code 2.1.325: hooks SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
    Stop, SubagentStart/Stop, SessionEnd; `additionalContext` output on all of these;
    `async: true` hooks; `agent_id`/`agent_type` inside sub-agents; `--settings`,
    `--setting-sources`, `--mcp-config --strict-mcp-config`, `--append-system-prompt-file`,
    `--disallowedTools`, `--allowedTools`, `--max-turns`; `-p --output-format stream-json
    --verbose`; `-p` without a permission mode denies calls that need approval. `--bare`
    ignores the subscription login and must never be used.
  - Codex CLI 0.154.0: hooks (stable) in `hooks.json` or `[hooks]`, project layer loaded
    only for a trusted project and reviewed once via `/hooks`; events SessionStart,
    UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStart/Stop, SessionEnd;
    `additionalContext` on SessionStart, UserPromptSubmit, Pre/PostToolUse; `async`;
    `[mcp_servers.<id>]`; `codex exec --json` (`thread.started`, `item.*`,
    `turn.completed` with token usage), `codex exec resume <id>`; AGENTS.md native;
    custom agents as TOML in `.codex/agents/`.
  - Vibe (not installed here; docs only): MCP servers (stdio, http) in `config.toml`;
    `hooks.toml` with hooks before and after tool calls and after each assistant turn,
    project hooks only in a trusted folder; `vibe --prompt` non-interactive with JSON
    output; sub-agents. No session-start or prompt hook found.

## 4. Approaches considered

- **A. Harness-neutral core with thin adapters (chosen by LFX, 15 Nov 2026).** Move the
  plugin logic into a Python core reached through one event contract; give each client a
  small adapter; add per-client executors for non-interactive work and one governor.
  Everything works everywhere; it is the largest change and touches Tao's plugins.
- B. Campaigns only: add `claude -p` and `codex exec` executors, keep interactive work on
  OpenCode. Fast, but the critic, retrieval and capture would not follow, and the
  interactive use LFX wants would stay OpenCode-only.
- C. Do it in Collegium's adapter boundary, leaving Magnolia untouched. No dependence on
  Tao's review, but Magnolia's own plugins (retrieval, critic) would have to be rebuilt
  in Collegium, and Magnolia alone would stay single-client.

## 5. Capabilities per client

| Magnolia behaviour | Core entry | OpenCode | Claude Code | Codex | Vibe |
|---|---|---|---|---|---|
| Rules + boot context + audit report | `session-start` | context hook (v2) | SessionStart `additionalContext`; fallback `--append-system-prompt-file` | SessionStart `additionalContext` | instructions file rendered at launch (P0 confirms the file) |
| Session-start retrieval directive | `session-start` | prompt rewrite | SessionStart `additionalContext` | same | in the instructions file |
| Doorbell `@agent` letters | `prompt` | prompt rewrite | letter written + `additionalContext` "already sent, do not act" (no rewrite possible) | same | not available (no prompt hook): `@agent` handled by an MCP tool `memory_send_letter` the rules point to |
| Action-time retrieval | `post-tool` | prepends to tool output | PostToolUse `additionalContext` | same | after-tool hook output (P0 confirms what reaches the model) |
| Tool-call capture | MCP `@captured` (unchanged) + `pre-tool`/`post-tool` ledger | yes | yes | yes | yes |
| Session mapping + Magnolia transcript | all events | yes | yes | yes | tool events + turn end only |
| Claim critic | `turn-end` (async) | yes | Stop, `async: true` | Stop, `async: true` | after-turn hook |
| Critic flags and job notices shown to the person | `prompt`, `post-tool` | toast | `systemMessage` / `additionalContext` at the next event | same | at next tool event |
| Sub-agent attribution | `agent` field | n/a | `agent_id`, `agent_type` | `agent_id`, `agent_type` on tool events (P0, observed in `spawn_agent`); SubagentStart/Stop not registered, unknown | P0 (not probed) |
| Bash disabled, `run_shell` instead | adapter config | `tools.bash:false` | `--disallowedTools Bash` | sandbox `workspace-write` + rule (P0: a PreToolUse hook `deny` on the Codex shell held; writes outside the writable roots were stopped by the sandbox) | tool disable in config (P0) |

### 5.1 What P0 observed

Source: `harness-probes-2026-11.md` (Claude Code 2.1.325, Codex 0.154.0, 15 Nov 2026). Vibe was
not probed (awaiting the owner's go to install it), so its column above is still a plan. What was
not observed is unknown, not absent.

- Tool names: MCP tools are `mcp__<server>__<tool>` on both clients. Claude adds an `mcp_server`
  object; Codex does not. Claude's sub-agent tool is `Agent`; Codex's is `spawn_agent`. The core
  ignores Claude's `ToolSearch` and `SubagentHandback` events.
- Failure: on Claude a failing MCP tool fires `PostToolUseFailure`, not `PostToolUse`; the adapter
  must subscribe to it. The Codex hook-side failure event is unknown.
- Sub-agents: on both clients tool events inside a sub-agent carry `agent_id` and `agent_type`;
  parent events carry neither. On Claude `SubagentStart`/`SubagentStop` fire; on Codex they were
  not registered.
- Ordering: Claude can fire `Stop` twice in one `-p` run (a background sub-agent hands back in a
  second turn), and `async: true` hooks can arrive after later events. The core must not treat
  `Stop` as the end of work or assume async order.
- Codex hook trust: project hooks do not load until trusted. The probes used
  `--dangerously-bypass-hook-trust` on throwaway temp projects only, at the owner's request. The
  flag is not to be put in any Magnolia config; the campaign worker project needs trusted hooks.
- Denial (Codex): a PreToolUse `deny` on the shell stopped the command and no PostToolUse followed;
  a write outside the project was stopped by the `workspace-write` sandbox. Claude denial was not
  probed.
- Context size: Claude accepted a 256 KB SessionStart `additionalContext` but the model reported
  seeing only a preview of about 2 KB (one self-report, threshold not measured). Codex delivered
  the tail marker of the same payload. The payload was 256 000 identical letters, which tokenise
  compactly, so this is weak evidence: a realistic payload is still to be measured. Until then
  the boot payload should be a short pointer plus a file or `--append-system-prompt-file` on Claude.

## 6. The core

### 6.1 `magnolia-hook`

A Python entry point in `compchem_memory` (`python -m compchem_memory.hook <harness>
<event>`): reads the client's raw hook payload on stdin, normalises it, runs the
behaviours for that event, and prints the client's native response. One module per
adapter does the two translations; behaviour modules never see client shapes.

Normalised event (`HookEvent`):

```text
harness        opencode | claude | codex | vibe
event          session-start | prompt | pre-tool | post-tool | turn-end | session-end
session_id     the client's session id
project_dir    resolved as today (MAGNOLIA_PROJECT_DIR, markers, .active-project)
prompt         user text (prompt)
tool           {name, server, raw_name, call_id, input, output, is_error} (pre/post-tool)
agent          {id, type} or null
assistant_text last assistant message when the client provides it (turn-end)
transcript_ref client transcript path, kept as a reference only
```

Tool names are canonicalised to `<server>.<tool>` (`compchem-tools.run_shell`) from
OpenCode's `compchem-tools_run_shell` and the `mcp__compchem-tools__run_shell` form of
Claude Code (Codex and Vibe forms recorded in P0). This replaces the hard-coded
`ACTION_TOOLS` list.

Normalised response:

```text
context        text the model should see now (additionalContext / system text)
notice         short line for the person (toast, systemMessage)
tool_note      text to attach to the tool result (action retrieval)
block          {reason} — used only to enforce campaign tool limits (section 8.1)
```

Each adapter maps the response to what its client accepts and drops the rest with a
log line, per the table in section 5.

### 6.2 Behaviours moved from TypeScript to Python

| From | To | Notes |
|---|---|---|
| boot-context.ts assembly | `hook.boot_context` | same files, same 256k cap |
| auto-retrieval.ts directive | `hook.retrieval_directive` | once per session, ledger unchanged |
| agent-doorbell.ts parsing and letters | `hook.doorbell` | same letter format; daemon unchanged |
| action-retrieval.ts trigger, noise control, ledgers, application probe | `hook.action_retrieval` | calls `quick_search` in-process; writes `surfacing.jsonl` through `surfacing.py` (removes the duplicated row schema) |
| claim-critic.ts trace building and judge | `hook.critic` | uses `llm.py` (one LLM client instead of two); trace from the Magnolia transcript (6.3) |
| session-capture.ts mapping | `hook.sessions` | `agent-sessions.jsonl` with `harness`; old `opencode-sessions.jsonl` still read |
| job-notify.ts drain | `hook.notices` | drained at the next event instead of a 30 s timer for clients without timers |

Hook start-up cost: one Python process per event. The current action-retrieval budget
is 2.5 s; the builder measures the cold start in P1 and keeps imports lazy.

### 6.3 Magnolia's own transcript

The hook stream writes `<project>/.magnolia/transcripts/<harness>/<session_id>.jsonl`:
user prompts, tool calls with inputs and outputs (truncated as today's critic does),
sub-agent markers, the assistant's intermediate and final text when the client exposes it (Claude Code
and Codex hooks give the final message; intermediate text is read from the tail of the
client transcript at turn end, best effort). Distillation,
handover and the critic read this file. A `TranscriptSource` interface keeps the current
OpenCode exporter chain as one implementation and adds Claude Code and Codex readers
only as fallbacks for assistant text, pinned by fixture tests because those formats are
internal.

## 7. Adapters and the launcher

`magnolia [--harness H] <project>`; the default comes from `llm-setup.json` key
`harness` (absent = `opencode`). Shared steps stay as they are: ports, tools daemon,
agents daemon, markers, venv, profiles, project scaffolding. The harness step renders
that client's configuration into `projects/<p>/.magnolia/harness/<H>/` from one neutral
description of MCP servers, instruction files, disabled tools and hooks, then starts
the client in the project directory.

- **OpenCode.** Unchanged in P1–P5. The TypeScript plugins may become shims over
  `magnolia-hook` in P6 if Tao agrees.
- **Claude Code.** `claude --setting-sources project,local --settings <rendered hooks>
  --mcp-config <rendered> --strict-mcp-config --disallowedTools Bash` started in the
  project directory. The rendered settings contain only Magnolia's hooks; the member's
  user settings keep their login. Never `--bare`.
- **Codex.** A project layer `projects/<p>/.codex/` with `config.toml` (MCP servers,
  sandbox) and `hooks.json`; the member trusts the project and reviews the hooks once
  (`/hooks`), which `magnolia setup` explains. No separate `CODEX_HOME`, so the login in
  the member's home is used as is and never copied.
- **Vibe.** Project `config.toml` and `hooks.toml` in a trusted folder; boot context and
  directives in the instructions file. Built only after P0 confirms the files.

Skills: `.opencode/skills/*/SKILL.md` is linked into `.claude/skills/` (same format);
Codex and Vibe skill locations are recorded in P0. Rules prose that names OpenCode
(bash disabled, `compchem-tools_*`, "restart opencode") becomes client-neutral, with a
short rendered note per client.

## 8. Non-interactive work and the usage governor

### 8.0 Routing agents and tasks to subscriptions

One person can send different agents and tasks to different subscriptions (LFX,
15 Nov 2026): for example the interactive session on Claude Code, campaign workers on
Codex, reviewers on Vibe, the `@literature` helper on OpenCode with a GLM Coding Plan.
Routing is a model id with a client prefix (8.1), set at four levels, the most specific
winning:

1. a campaign stage (`model`, `alternate_model`, `hard_stage_model`, as today);
2. a helper agent's `projects/<agent>/agent.json` (`model`);
3. a role default in `llm-setup.json` (`routes: {interactive, worker, reviewer, helper,
   evaluator}`), written by `magnolia setup`;
4. the global default (`harness` + `main_model`, as today).

`magnolia usage` shows which route each role resolves to. Reviewer independence still
compares model families, so routing a worker and its reviewer to two vendors satisfies
it.

### 8.1 Executors

`WorkerFactory` returns an executor chosen by the model id prefix: `claude-code/<model>`,
`codex/<model>`, `vibe/<model>`; any other id stays OpenCode. All return the existing
`WorkerResult`.

**Campaign limits carry over unchanged** (*builder check, settled by the author*). Today
`.opencode/agents/campaign-worker.md` denies everything by default, allows reads except
`*.env*` and `.magnolia/campaign-service.{json,log}`, allows edits only under `work/**`,
three memory read tools and no shell; `campaign-reviewer.md` allows no edits and no
commands. These rules move into one neutral agent definition
(`agents/campaign-{worker,reviewer}.yaml`) rendered per client, and a client that
cannot enforce them does not run that role:

- `ClaudeCodeExecutor`: `claude -p --output-format stream-json --verbose` with the same
  rendered settings and MCP config; `--tools`/`--allowedTools` limited to the role's
  tools; path rules enforced by a PreToolUse hook that returns `block` for reads of the
  excluded files and for edits outside `work/**`; `--max-turns` from the stage's
  `max_steps`; `--resume` for the same-session retry. Parses `system/init`, assistant
  and tool events, `result`.
- `CodexExecutor`: reviewer role with `-s read-only`; worker role only if P0 shows that
  a PreToolUse hook can deny Codex's shell and out-of-scope writes (otherwise Codex is
  refused as a campaign worker and stays available as reviewer and helper agent).
  `codex exec --json` in the project with its `.codex/` layer, `codex exec resume <id>`
  for retries. Parses `thread.started`, `item.completed`, `turn.completed` usage.
- `VibeExecutor`: `vibe --prompt` with JSON output, after P0, under the same rule.

Stage type `"opencode"` gains the alias `"agent"`; `model_family` learns the prefixes so
reviewer independence works across clients (a Codex worker can be reviewed by Claude).
The agents daemon and the router evaluator use the same registry, which removes their
duplicated event parsers. The evaluator's existing `JevEvaluator._api_key()` reads
OpenCode's `auth.json` and the OAuth token in `opencode-v2.db` to call `opencode.ai`
directly; that conflicts with anti-goal A1 but is Tao's router code and outside this
design. It is reported to Tao as a separate item and left unchanged here (*builder
check, settled by the author*).

### 8.2 Governor

Every non-interactive run on a subscription (campaign worker, reviewer, daemon helper
agent, router evaluation) asks the governor for a slot first, **OpenCode runs
included** (LFX, 15 Nov 2026). Interactive sessions are not throttled: the person is the
rate.

Budgets are kept per subscription, not per client: the key is the client plus, for
OpenCode, the provider id (`opencode:openai`, `opencode:zai-coding-plan`,
`opencode:kimi-for-coding`, `claude-code`, `codex`, `vibe`). Magnolia cannot tell from
an OpenCode provider id whether it is a subscription login or an API key without reading
credentials (anti-goal A1), so the policy file declares it: providers listed under
`api_key_providers` are only recorded in the ledger; every other provider, including an
unknown one, is treated as a subscription. This changes today's behaviour: OpenCode
campaigns, daemon helpers and router evaluations wait for a governor slot (anti-goal
A4 is relaxed here by LFX's decision; the change is announced in the release notes and
`magnolia usage` shows waiting runs).

- Policy file `~/.config/magnolia/usage-policy.yaml`, per subscription key: `max_concurrent`
  (default 1), `max_runs_per_day` (default 20), `max_turns_per_run` (default 40),
  `min_seconds_between_runs` (default 60). Defaults are the builder's proposal and LFX's
  to confirm.
- Limit signals end the run without retry: Claude `system/api_retry` with
  `rate_limit` or `billing_error`, result text "You've hit your session limit" or
  "weekly limit"; Codex `turn.failed` or `error` events (texts recorded in P0); Vibe in
  P0. The stage goes to a new state `waiting_quota` with the reset time when the text
  gives one, and resumes only after that time or on a person's command.
- Ledger `~/.local/state/magnolia/usage-ledger.jsonl`: client, model, run kind, project,
  turns, tokens, duration, outcome. `magnolia usage` prints the day and week.
- API-key routes (memory model, critic judge) are separate billing and stay outside the
  governor.

## 9. Phases and proof

| Phase | Content | Proof |
|---|---|---|
| P0 (done, c7596eb, 75b0aa6, c6c9db4) | Probes, no Magnolia change: each client's hooks around an MCP tool call and inside a sub-agent, tool-name format, size limit of injected context, Codex shell-tool control, Vibe install and files, usage-limit texts where documented | probe report in `opencode_cc_mem/docs/` with commands and captured payloads |
| P1 (done, b2311d9 to 1345638) | `magnolia-hook`, normalised schema, Python behaviours, Magnolia transcript; OpenCode plugins untouched | parity tests: on recorded OpenCode payloads the core produces what the TS plugins produce; existing suites green |
| P2 | Claude Code adapter + `--harness` launcher + `setup` support | live session on a demo project: boot context visible, retrieval note after a `run_shell` call, capture rows, critic row, transcript file; distillation of that session compared with an OpenCode session on the same task (same candidate kinds found) |
| P3 | Codex adapter | same live checklist |
| P4 | Executor registry, governor, campaigns and daemon on Claude Code and Codex | unit tests on recorded event streams; one small campaign per client; governor stops on a simulated limit |
| P5 | Vibe adapter and executor | live checklist on Vibe |
| P6 | Optional: OpenCode plugins as shims over the core | Tao's call |

Status of P0 and P1 (branch `feat/hook-core-p0-p1`, nothing pushed, awaiting Tao's review):

- P0: Claude Code and Codex probed; Vibe not probed. Findings in section 5.1; usage-limit texts
  not recorded (no limit hit, none found in the help texts consulted).
- P1: package `compchem_memory.hook` (events, project, state, boot_context, directive, doorbell,
  action_retrieval, transcript, sessions, critic, notices, dispatch, `__main__`, JSON adapter).
  Full package suite at 1345638: 1046 passed, 4 skipped. Cold start, median of 5 on one Mac:
  0.041 s for session-start, 0.078 s for post-tool.
- Not done: the OpenCode plugins are untouched and still run the TypeScript path; distillation
  and handover do not yet read the Magnolia transcripts (section 6.3, Plan 2); no client adapter
  or launcher exists yet (P2 onward).

Each phase ends with Tao's review on its own branch.

## 10. Decided so far

- Approach A (LFX, 15 Nov 2026).
- Clients: OpenCode, Claude Code, Codex, Vibe (LFX, 15 Nov 2026).
- One person may route different agents and tasks to different subscriptions of their
  own (LFX, 15 Nov 2026; section 8.0).
- The governor covers OpenCode's non-interactive runs on subscriptions too (LFX,
  15 Nov 2026; section 8.2).
- Subscriptions are used only through the vendors' own clients; no credential handling
  by Magnolia (LFX's "sans abuser l'abonnement", made concrete by anti-goals A1–A2).

## 11. Interfaces seen by others

`HookEvent`, the hook entry point and the Magnolia transcript are internal to Magnolia
in P1–P5: they carry a `schema_version` field and are documented, but Collegium's use
of them (as a library, through its own client configuration, or by reading the files)
is decided later in Collegium. The Claude Code adapter adds Magnolia's hooks through
`--settings` without disabling project settings, so hooks another tool places in the
project's `.claude/settings.json` still run (*builder check, settled by the author*).

## 12. Left to the builder

- Module layout inside `compchem_memory` and the adapter translation code.
- Exact rendered config files per client, within section 7.
- Transcript truncation limits (keep the critic's current caps).
- Whether to add a long-lived hook process if P1 measures cold starts above budget.
- Test fixtures: record real payloads in P0 and replay them.
