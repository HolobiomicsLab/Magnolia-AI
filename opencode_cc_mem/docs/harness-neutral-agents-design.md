# Harness-neutral Magnolia: one core, four agent harnesses

Status: **Approved by LFX** (2026-11-15); P0 and P1 built on `feat/hook-core-p0-p1`, on top of
`design/harness-neutral-agents` and `feat/llm-providers-mistral-zai`. Product owner: LFX. Repository owner and
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
  - Mistral Vibe 3.1.2 (probed in P0): MCP servers in `.vibe/config.toml`; hooks in
    `.vibe/hooks.toml` of types `session_start`, `session_end`, `pre_agent` (carries the
    prompt), `post_agent`, `pre_tool`, `post_tool`, `notification`; project files are read
    only from a trusted folder (`--trust` per run); `vibe -p --output json` non-interactive,
    `--max-turns`, `--max-price`, `--output-dir` (an `export.json` with usage, cost and
    outcome); sub-agents through `agents.spawn`. No sandbox.

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
| Rules + boot context + audit report | `session-start` | context hook (v2) | static rules through `--append-system-prompt-file`; capped dynamic SessionStart `additionalContext` | rules in `.codex/config.toml` `developer_instructions`; Codex reads `AGENTS.md` natively; capped dynamic SessionStart `additionalContext` | `session_start` `additional_context` (P0: 256 KB reached the model) + `AGENTS.md` |
| Session-start retrieval directive | `session-start` | prompt rewrite | SessionStart `additionalContext` | same | `session_start` `additional_context` |
| Doorbell `@agent` letters | `prompt` | prompt rewrite | letter written + `additionalContext` "already sent, do not act" (no rewrite possible) | same | same, on `pre_agent` (carries the prompt) |
| Action-time retrieval | `post-tool` | prepends to tool output | PostToolUse `additionalContext` | same | built-ins use `post_tool` `additional_context`; MCP notes are carried to the same agent's next built-in event, or the main agent's next prompt |
| Tool-call capture | MCP `@captured` (unchanged) + `pre-tool`/`post-tool` ledger | yes | yes | yes | yes |
| Session mapping + Magnolia transcript | all events | yes | yes | yes | yes; assistant text only from the client transcript (`post_agent` carries none) |
| Claim critic | `turn-end` | yes | synchronous Stop recovers replies and starts a detached worker | Stop hook | `post_agent` defers recovery; the next main prompt or session end starts a detached worker |
| Critic flags and job notices shown to the person | `prompt`, `post-tool` | toast | `systemMessage` / `additionalContext` at the next event | same | `additional_context` at the next `pre_agent` or `post_tool` |
| Sub-agent attribution | `agent` field | n/a | `agent_id`, `agent_type` | `agent_id`, `agent_type` on tool events (P0, observed in `spawn_agent`); SubagentStart/Stop not registered, unknown | child `session_id` + `parent_session_id`, `agent_name` |
| Bash disabled, `run_shell` instead | adapter config | `tools.bash:false` | `--disallowedTools Bash` | sandbox `workspace-write` + rule (P0: a PreToolUse hook `deny` on the Codex shell held; writes outside the writable roots were stopped by the sandbox) | `--disabled-tools file_system.bash` + a `strict` `pre_tool` deny (P0: no sandbox; an out-of-project write succeeded) |

### 5.1 What P0 observed

Source: `harness-probes-2026-11.md` (Claude Code 2.1.325, Codex 0.154.0, Mistral Vibe 3.1.2,
15 Nov 2026). What was not observed is unknown, not absent.

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
- Model/config fields observed in P0: Claude's selected model was supplied by `--model` and its
  result named models under `model_usage_since_last_result` (the older scrubber dropped
  `modelUsage`); Codex received `-m` and repeated the actual model in hook payloads such as
  `SessionStart`; Vibe reported its actual model in `session_start` and sub-agent models in
  `export.json`. Project MCP/hook configuration was read from Claude's run-owned settings/MCP
  files, Codex `.codex/config.toml` plus `.codex/hooks.json`, and Vibe `.vibe/config.toml` plus
  `.vibe/hooks.toml`. These observations do not prove role isolation or normal trust.
- Denial (Codex): a PreToolUse `deny` on the shell stopped the command and no PostToolUse followed;
  a write outside the project was stopped by the `workspace-write` sandbox. Claude denial was not
  probed.
- Context size: Claude accepted a 256 KB SessionStart `additionalContext` but the model reported
  seeing only a preview of about 2 KB (one self-report, threshold not measured). Codex delivered
  the tail marker of the same payload. The payload was 256 000 identical letters, which tokenise
  compactly, so this is weak evidence: a realistic payload is still to be measured. Until then
  the boot payload should be a short pointer plus a file or `--append-system-prompt-file` on Claude.
- Vibe: all seven hook types fire. MCP tools are `mcp_<server>.<tool>`, the shell is
  `file_system.bash`, the sub-agent tool `agents.spawn`; a child's events carry its own
  `session_id` and the parent's in `parent_session_id`. A failing tool is an ordinary `post_tool`
  with `tool_status: "failure"`. Headless MCP calls are denied without `--auto-approve`, and
  `--enabled-tools mcp_...` hides the MCP tool, so non-interactive runs use `--auto-approve` with
  explicit `--disabled-tools`. A `pre_tool` deny stops the shell, but there is no sandbox: an
  out-of-project write succeeded. A 256 KB `session_start` context reached the model (about 63 k
  extra input tokens). `export.json` gives tokens, cost, outcome and stop reason per run and per
  sub-agent; the price cap was not exercised (free default model). Two sub-agents were reported
  `failed` although each finished its call (cause unknown).
- Prompts: the doorbell, the session-start directive and the notices act only on the person's
  prompts. A prompt event that carries a sub-agent (`agent` set) is captured in the transcript and
  otherwise ignored, so a helper's instructions never ring a letter or receive the person's notices.

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
  project directory. The rendered settings contain Magnolia's hooks and the MCP
  `permissions.allow` entries they require; the member's user settings keep their login.
  Never `--bare`.
- **Codex.** A project layer `projects/<p>/.codex/` with `config.toml` (MCP servers,
  sandbox) and `hooks.json`; the member trusts the project and reviews the hooks once
  (`/hooks`), which `magnolia setup` explains. No separate `CODEX_HOME`, so the login in
  the member's home is used as is and never copied.
- **Vibe.** Project `config.toml` and `hooks.toml` in a trusted folder; boot context and
  directives in the instructions file. Built only after P0 confirms the files.

Skills: `.opencode/skills/*/SKILL.md` is linked into `.claude/skills/` and the shared
`.agents/skills/` location used by Codex and Vibe. Rules prose that names OpenCode
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

`magnolia usage` shows which route each role resolves to. Reviewer independence compares the
resolved model-family field, not vendor or client shorthand. Different vendors satisfy it only
when their resolved families differ; the route prefix is stripped before family classification.

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
- `CodexExecutor`: D2 refuses both worker and reviewer unless Task 0 proves the complete role
  matrix through normal persisted trust: permitted reads, denied secret reads, no shell and
  confined worker edits; reviewer additionally permits no edits. `-s read-only` or changing the
  working directory is not sufficient. Helper/evaluator remain separate contracts.
  `codex exec --json` in the project with its `.codex/` layer, `codex exec resume <id>`
  for evidenced retries. Parses `thread.started`, `item.completed`, `turn.completed` usage.
- `VibeExecutor`: `vibe -p --output json --auto-approve --output-dir <run>` with
  `--disabled-tools` for the shell and a `strict` `pre_tool` hook enforcing the role's path
  rules, since Vibe has no sandbox (P0). D4 permits guarded worker edits under its own
  `work/**` only after the complete Task 0/6 gate passes; reviewer, helper and evaluator keep
  writes disabled. `--max-turns` and optional `--max-price` come from proved policy; usage and
  outcome are read from `export.json`.

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
| P0 (done) | Probes, no Magnolia change: each client's hooks around an MCP tool call and inside a sub-agent, tool-name format, size limit of injected context, Codex shell-tool control, Vibe install and files, usage-limit texts where documented | probe report in `opencode_cc_mem/docs/` with commands and captured payloads |
| P1 (done) | `magnolia-hook`, normalised schema, Python behaviours, Magnolia transcript; OpenCode plugins untouched | parity tests: on recorded OpenCode payloads the core produces what the TS plugins produce; existing suites green |
| P2 | Claude Code adapter + `--harness` launcher + `setup` support | live session on a demo project: boot context visible, retrieval note after a `run_shell` call, capture rows, critic row, transcript file; distillation of that session compared with an OpenCode session on the same task (same candidate kinds found) |
| P3 | Codex adapter | same live checklist |
| P4 | Executor registry, governor, campaigns and daemon on Claude Code and Codex | unit tests on recorded event streams; one small campaign per client; governor stops on a simulated limit |
| P5 | Vibe adapter and executor | live checklist on Vibe |
| P6 | Optional: OpenCode plugins as shims over the core | Tao's call |

Status of P0 and P1 (branch `feat/hook-core-p0-p1`, nothing pushed, awaiting Tao's review):

- P0: Claude Code, Codex and Vibe probed. Findings in section 5.1; usage-limit texts not
  recorded (no limit hit, none found in the help texts consulted).
- P1: package `compchem_memory.hook` (events, project, state, boot_context, directive, doorbell,
  action_retrieval, transcript, sessions, critic, notices, dispatch, `__main__`, JSON adapter).
  Overlapping post-tool hooks of one session reserve their retrieval budget at selection, so
  parallel tool calls cannot exceed the per-entry and per-session caps. Cold start, median of 5
  on one Mac: 0.041 s for session-start, 0.078 s for post-tool.
- Not done in P1: the OpenCode plugins are untouched and still run the TypeScript path.

Status after Plan 2 (branch `feat/harness-adapters-p2`, stacked on P0/P1, nothing pushed):

- P2, P3 and the Vibe part of P5 (adapters, `magnolia --harness claude|codex|vibe`, `setup
  --harness`, distillation and handover from every client) are built and tested: package suite
  1473 passed, 4 skipped.
- Live acceptance on 15 November 2026: `opencode_cc_mem/docs/harness-acceptance-2026-11.md`.
  Boot context, action notes, session rows and the member's own hook were seen in all three
  clients. Open: the critic judge returned no JSON object in two of three calls. Claude's Stop
  now launches a detached worker, injected action notes are excluded from distillation, and Vibe
  carries MCP action notes to a later deliverable event.
- Not done: P4 (executors, governor), the Vibe executor, OpenCode itself live (not installed on
  the test Mac).

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
- D1: per-subscription defaults are `max_concurrent=2`, `max_runs_per_day=50`,
  `max_turns_per_run=60`, `min_seconds_between_runs=30`; effective role caps remain the
  minimum of configured and governor caps.
- D2: Codex worker and reviewer are refused unless their complete Task 0/6 gates pass.
- D3: Vibe memory uses a project-bound loopback HTTP service; API credentials stay only in
  the service environment.
- D4: Vibe worker edits may be guarded under its own work tree only after the full path gate;
  until then the worker is refused.

### Accepted corrections from the Plan 3 live gates

- Claude worker MCP visibility is accepted with **hook-only allowlist enforcement**. Claude Code
  exposes all tools registered by the memory server, while Magnolia's required PreToolUse hook
  denies every non-allowlisted call. The owner accepted that enforcement boundary after its
  production fail-closed fallback and denial path were observed live. Server-side tool filtering
  remains a context/turn-efficiency follow-up, not an executor gate. Claude reviewer, helper and
  evaluator passed their live role gates outright.
- Capability evidence binds to the client binary identity and a normalized rendered
  template/policy shape, not to the disposable probe project path or its run-specific digest.
  Runtime discovery must resolve to the launching project. A changed client version/SHA-256,
  renderer output shape, hook command or required-policy schema makes the evidence stale. The
  four-role HEAD comparison accepted the sole Task 8b delta, the added
  `MAGNOLIA_HOOK_POLICY_SHA256` environment name; all rendered files, policy bodies, hook commands
  and Claude worker stdio memory routing stayed byte-identical to the `02b5f43` evidence.
- SessionStart currently injects boot context into every Claude role. The reviewer cap of eight is
  consequently tight (two turns were spent looking for unavailable memory tools). Role-selective
  boot injection is a follow-up, not a blocker.
- Claude production workers use the observed stdio memory-server shape, including `${VAR}`
  expansion for named server environment entries; unauthenticated HTTP memory routes are refused.
  The launch binding measures required policy names from the effective environment, hashes the hook
  package plus required-mode wrapper, and binds Magnolia's venv-interpreter selection rule rather
  than a machine path. Turn caps are bound as the positive `min(role, governor)` rule, not as probe
  values. The Claude child receives only the observed base variables, Magnolia policy bindings, its
  own subscription token and standard proxy/CA variables; endpoint/runtime redirects are named and
  refused.
- The executor records and spawns the absolute real path whose SHA-256 passed the gate, then hashes
  it again immediately before process spawn. A residual TOCTOU interval remains between that final
  file read and the kernel exec; eliminating it would require descriptor-based execution supported
  end to end by the governed-process barrier.

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
