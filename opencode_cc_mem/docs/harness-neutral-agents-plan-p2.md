# Harness-neutral Magnolia — Plan 2: client adapters and `magnolia --harness`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `magnolia --harness claude|codex|vibe <project>` opens an interactive session in that client, signed in with the member's own login. Magnolia's MCP servers, rules, boot context and hooks are present, and the sessions are distilled like OpenCode's. `magnolia <project>` without the flag keeps doing exactly what it does today.

**Architecture:**
- **Adapters.** One adapter per client translates its hook payloads to and from the neutral `HookEvent`/`HookResponse` of Plan 1.
- **Reply text.** Small readers recover the agent's reply text from each client's own transcript.
- **Configuration.** A package `compchem_memory.harness` reads the MCP servers from `opencode.json.template`, the one existing description. It merges Magnolia's entries into each client's configuration without touching the member's own, and prints the client command line, which the Bash launcher executes.
- **Distillation.** It lists sessions through one identity contract (`SessionRef`). Hook-fed sessions are exported from Magnolia's own transcripts in the OpenCode export shape, so the existing distiller runs unchanged.

**Tech Stack:** Python 3.11+ (`compchem-memory`), pytest, Bash 3.2 (macOS `/bin/bash`), Claude Code 2.1.325, codex-cli 0.154.0, Mistral Vibe 3.1.2.

**Spec:** `opencode_cc_mem/docs/harness-neutral-agents-design.md` (approved by LFX on 15 Nov 2026), sections 5, 6.3 and 7, and phases P2, P3 and the interactive half of P5.
- Evidence: `opencode_cc_mem/docs/harness-probes-2026-11.md` and the fixtures in `opencode_cc_mem/mcp-servers/compchem-memory/tests/fixtures/hooks/{claude,codex,vibe}/`.
- Code facts: `.superpowers/sdd/harness-neutral-agents-plan-p2/inventory.md` (git-ignored).

**Audit:** Codex (Astra, max, two Sol leaves) audited the first version (bb77bff) on 15 Nov 2026. Verdict: "build after these fixes", with three Blockers and nine Important findings, all folded in below. Run record: `~/.codex-runs/magnolia/plan2-audit-001/result.json`.

**Deferred to Plan 3:**
- routing (`routes` in `llm-setup.json`);
- the campaign, reviewer, helper-agent and evaluator executors, and the usage governor;
- campaign path rules enforced by `pre-tool` blocks.

The agents daemon and `magnolia-exp` keep rendering `opencode.json` themselves until then.

## Global Constraints

- **Paths:** `src/…` and `tests/…` are relative to `opencode_cc_mem/mcp-servers/compchem-memory/`. Other paths are relative to the repository root.
- **The OpenCode path does not change.** With no `--harness` flag and no `harness` key, `magnolia [--profile P] <project>` must:
  - render a byte-identical `projects/<p>/opencode.json`;
  - run the same OpenCode binary checks and version call;
  - execute the same command, in the same directory, with the same environment.

  Task 1 pins this with a characterisation test before any other launcher change. Nothing in `.opencode/plugins/` changes (design G5, A4).
- **Credentials (design A1).** Magnolia never reads, copies or moves a client's login:
  - no `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `VIBE_HOME` override;
  - no copy of `auth.json` or the keychain;
  - never `claude --bare`.

  Never put `--dangerously-bypass-hook-trust`, `--dangerously-skip-permissions`, `--auto-approve` or `--trust` in a rendered configuration or an interactive command line. Reading a client's session transcript through the `transcript_path` its hook hands over is allowed: it is not a credential.
- **The CLI fails open** (Plan 1): it exits 0 and prints nothing on error, with one exception, the shell policy (Task 2), which fails closed. An event the adapter ignores prints nothing and writes no transcript row.
- **Collegium:** the names it reads stay as they are (Plan 1, "Alignment with Collegium"): `opencode-sessions.jsonl` and its key `opencode_session_id`, `opencode-transcripts/`, `opencode-distilled/` (directory and existing file names), and the provenance fields of staged entries. New files sit beside them.
- **Member configuration is preserved.** In `.codex/config.toml`, `.codex/hooks.json`, `.vibe/config.toml` and `.vibe/hooks.toml`, Magnolia owns exactly these entries:
  - the MCP servers named `compchem-tools`, `compchem-memory` and `perspicacite`;
  - hooks whose command contains `-m compchem_memory.hook`;
  - Vibe hooks whose `name` starts with `magnolia-`.

  Every other entry survives a render unchanged, and rendering twice gives the same files. Claude's Magnolia files live under `.magnolia/harness/claude/` and are passed by flag, so the project's own `.claude/settings.json` is never written.
- **Bash 3.2:** no `mapfile`, no `declare -A`, no `${var,,}`.
- **Rendered files** stay under `projects/<p>/` and are written atomically (temporary file, then replace). Nothing under `~/.claude`, `~/.codex` or `~/.vibe` is written.
- **Hook commands** are `<absolute venv python> -m compchem_memory.hook <harness> <event>`. Timeouts: 10 s for post-tool, 30 s for the rest. Turn-end runs asynchronously wherever the client documents it (Claude `async: true`; Codex only if its bundled documentation shows the key).
- **Dry run** (`MAGNOLIA_DRY_RUN=1`) prints only an allowlist of variables: `MAGNOLIA_PROJECT_DIR`, `MAGNOLIA_PROJECT_WORKSPACE`, `MAGNOLIA_WORKSPACE_ROOT`, `MAGNOLIA_HOOK_POLICY`, `MAGNOLIA_OPENCODE_V2`, `COMPCHEM_TOOLS_PORT`, `MCP_TOOL_TIMEOUT`. It never prints a variable whose name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD`.
- **Live runs:** at most ten short calls per client on the member's subscription, all on a demo project under `$TMPDIR`. Vibe runs set `VIBE_DISABLE_TELEMETRY=true`. Codex hook trust for the demo project, and any memory-model or critic call, need LFX's go at execution time.
- **Tests:** package tests run with `python -m pytest -q tests` from the package directory. Launcher tests (`opencode_cc_mem/tests/test_launcher_harness.py`) run from the repository root with stub client binaries on `PATH`. Baseline at 6bac3aa: 1081 passed, 4 skipped. Each task records the counts it observed. Existing tests are not edited unless the brief names them.

## Review Focus

1. **The member's own project configuration:** a `.claude/settings.json` hook, or Codex and Vibe servers and hooks of their own, keep running beside Magnolia's. Tests: Task 6 `test_render_preserves_member_entries_and_is_idempotent`; Task 7 `test_claude_argv_keeps_project_settings`; live check in Task 13.
2. **A client binary that is missing, with OpenCode absent too:** the launcher names the missing binary and exits 1 before any daemon starts, and a present client launches without any OpenCode binary installed. Test: Task 9 `test_claude_launch_without_any_opencode_binary`, `test_missing_client_binary_exits_before_daemons`.
3. **A project path with spaces, quotes or non-ASCII characters** in the rendered JSON and TOML. Test: Task 6 `test_render_quotes_awkward_paths`.
4. **Moving between clients on one project** (OpenCode, then Claude, then OpenCode; a resumed Claude session): no active session is ever sealed, every session is distilled once, and legacy-only stores behave exactly as today. Tests: Task 12 `test_switching_clients_never_seals_an_active_session`, `test_legacy_only_store_unchanged`.
5. **Parallel and late hooks:** a notice is shown once, and an asynchronous Stop that arrives after the next prompt judges its own turn's report. Tests: Task 2 `test_parallel_events_show_a_notice_once`, `test_late_stop_judges_its_own_turn`.
6. **Credentials stay hidden:** a dry run, an error row and a rendered file never contain a key. Test: Task 9 `test_dry_run_hides_sentinel_credentials`.

---

### Task 1: Pin the OpenCode launch (characterisation first)

**Files:**
- Modify: `opencode_cc_mem/softwares/bin/magnolia`, to add only the dry-run switch.
- Create: `opencode_cc_mem/tests/test_launcher_harness.py` and `opencode_cc_mem/tests/stubs/{opencode,opencode-v2,claude,codex,vibe}`. Each stub prints `{"argv", "cwd"}` as JSON and exits 0; `--version` prints `stub 0.0`.

**Interfaces:**
- Produces: `MAGNOLIA_DRY_RUN=1` reaches the exec line and prints one JSON object instead of executing: `argv`, `cwd`, and `env`, restricted to the allowlist in the Global Constraints. It starts no daemon. A helper `run_launcher(tmp_workspace, args, env) -> dict` in the test module copies the launcher's inputs (template, `rules/`, `AGENTS.md`, a project) into a temporary workspace.

- [ ] **Step 1: Write the tests** `test_opencode_launch_unchanged` (argv `[<v2 stub>]`, cwd `projects/<p>`, env allowlist values) and `test_opencode_config_snapshot`. The snapshot is the rendered `opencode.json`, stored as `opencode_cc_mem/tests/snapshots/opencode.json` with the temporary paths replaced by `@@TMP@@`.
- [ ] **Step 2: Add the dry-run branch.** It is the only change. Run the tests, record the snapshot, then commit `test(launcher): pin the OpenCode launch before adding harnesses`. The snapshot file must not change in any later task.

### Task 2: Core changes the adapters need

**Files:**
- Modify: `src/compchem_memory/hook/events.py` (`canonical_tool`), `hook/adapters/__init__.py`, `hook/__main__.py`, `hook/dispatch.py`, `hook/notices.py`, `hook/critic.py`
- Create: `src/compchem_memory/hook/policy.py`
- Test: `tests/hook/test_events.py`, `tests/hook/test_policy.py`, `tests/hook/test_notices.py`, `tests/hook/test_critic.py`, `tests/hook/test_cli.py`

**Interfaces:**
- Produces:
  - `canonical_tool` handles the Vibe forms.
  - `Adapter.parse(raw, event) -> HookEvent | None`, where `None` means "ignore this event".
  - `policy.decide(event, env) -> Block | None`.
  - `run_critic(paths, harness, session_id, agent_id, turn_id=None)`.
  - `notices.release_notices(paths, harness, session_id, notices)`.

- [ ] **Step 1: Tool names.** Add `test_canonical_tool_vibe_forms`: `mcp_compchem-tools.run_shell` → `("compchem-tools", "run_shell")`, `file_system.bash` → `(None, "bash")`, `file_system.write_file` → `(None, "write")`, `file_system.edit` → `(None, "edit")`, `agents.spawn` → `(None, "agents.spawn")`. `Bash` gives `(None, "bash")` on every client. Implement: the `mcp_` prefix is checked after `mcp__` and split on the first `.`; `file_system.<x>` maps `write_file` to `write` and keeps the other names.
- [ ] **Step 2: Shell policy, failing closed.**
  - When `MAGNOLIA_HOOK_POLICY` names a file, `policy.decide` reads its `deny_tools` list. It returns `Block(REASON)` on `pre-tool` for a denied canonical name.
  - When the variable is set but the file cannot be read, or holds bad JSON, it denies `bash`.
  - When the variable is unset, it denies nothing.
  - `REASON`, verbatim: `Magnolia disables this client's built-in shell. Use the compchem-tools run_shell tool instead; it records the command for the project's memory.`
  - `__main__` evaluates the policy for `pre-tool` before `handle`, inside its own guard. If anything in the policy evaluation raises and the variable is set and the tool canonicalises to `bash`, it renders that block anyway. The policy result is merged into the response.
  - Tests: `test_policy_blocks_bash_on_pre_tool`, `test_unreadable_policy_still_blocks_bash`, `test_no_policy_blocks_nothing`, `test_policy_ignores_post_tool`, `test_cli_policy_block_survives_handle_failure` (handle monkeypatched to raise).
- [ ] **Step 3: Ignored events.** `__main__` treats `parse(...) is None` as "exit 0, print nothing, write nothing". Test: `test_ignored_event_writes_no_row`.
- [ ] **Step 4: Notices reserved like action notes.** `pending_notices` moves what it returns into `in_flight` under the lock. `commit_notices` drops them; `release_notices` puts them back, and the CLI calls it when `notice` was not emitted or a failure occurred. Test: `test_parallel_events_show_a_notice_once`.
- [ ] **Step 5: Isolate commit failures.** A failure inside `_commit_delivered` is logged and the rendered exit code is kept. Test: `test_commit_failure_keeps_block_exit_code`.
- [ ] **Step 6: The critic judges the turn that emitted the event.** When `turn_id` is given and the transcript rows carry `turn_id`, the boundary and the report are taken from the rows of that turn only. Otherwise the critic keeps today's behaviour. `dispatch._turn_end` passes `event.turn_id`, and so does `probe_application`. Test: `test_late_stop_judges_its_own_turn` has two prompts and two assistant rows, and the Stop for turn 1 arrives after prompt 2.
- [ ] **Step 7: Run `tests/hook` and the full suite.** Commit `feat(hook): Vibe tool names, fail-closed shell policy, ignored events, reserved notices, per-turn critic`.

### Task 3: Recover the agent's reply text from client transcripts

**Files:**
- Create: `src/compchem_memory/hook/client_transcripts.py`
- Create: `tests/fixtures/client-transcripts/{claude,codex,vibe}/…`. The lead records these in Step 1, scrubbed with `scripts/probes/make_fixtures.py`'s rules.
- Test: `tests/hook/test_client_transcripts.py`

**Interfaces:**
- Produces: `assistant_texts(harness: str, transcript_ref: str, *, turn_id: str | None, agent_id: str | None, max_bytes: int = 2_000_000) -> list[str]`.
  - It reads the last `max_bytes` of the client file and returns the assistant text blocks of the given turn, oldest first, scrubbed with `scrub_secrets`.
  - It returns `[]` for any unreadable or unknown shape, and never raises.
  - Claude: the JSONL at `transcript_path`, and for a sub-agent the `agent_transcript_path` from SubagentStop.
  - Codex: the rollout JSONL at `transcript_path`.
  - Vibe: the session file at `transcript_path`.
- `dispatch._turn_end`, and Vibe's `post_agent` turn-end, append one `assistant` transcript row per recovered text block that is not already recorded. The existing `assistant_text` stays the last block.

- [ ] **Step 1 (lead, live: one call per client, within budget):** run one short session per client on a temp project with Plan 1's probe hooks, logging `transcript_path`. Copy each client transcript into `tests/fixtures/client-transcripts/<client>/`, scrubbed. Note: Claude and Codex keep these files under `~/.claude/projects/` and `~/.codex/sessions/`; only the file named by `transcript_path` is read.
- [ ] **Step 2: Tests**, one per client. Each checks that intermediate and final texts come out in order, that a sub-agent's text is attributed to the sub-agent, and that a truncated, garbage or missing file gives `[]`. Add `test_vibe_turn_end_records_reply_text` at the dispatch level.
- [ ] **Step 3: Implement. Commit** `feat(hook): recover reply text from each client's transcript`.

### Task 4: Claude Code adapter

**Files:**
- Create: `src/compchem_memory/hook/adapters/claude.py`; register it in `hook/adapters/__init__.py`
- Test: `tests/hook/test_adapter_claude.py`

**Interfaces:**
- Event map, from native name to CLI event:
  - SessionStart → `session-start`
  - UserPromptSubmit → `prompt`
  - PreToolUse → `pre-tool`
  - PostToolUse and PostToolUseFailure → `post-tool`
  - Stop → `turn-end`
  - SubagentStart → `agent-start`
  - SubagentStop → `agent-end`
  - SessionEnd → `session-end`
- The reverse map used for rendering comes from `event.event`. For `post-tool` it is `PostToolUseFailure` when `event.tool.is_error`, and `PostToolUse` otherwise.

- [ ] **Step 1: Replay tests on every fixture under `claude/`.**
  - Fields map as follows: `session_id`; `cwd`; `transcript_ref` ← `transcript_path` (← `agent_transcript_path` on SubagentStop); `turn_id` ← `prompt_id`; `agent` ← `agent_id`/`agent_type`; tool raw name ← `tool_name`; call id ← `tool_use_id`; input ← `tool_input`; output ← `normalise_output(tool_response)`.
  - On PostToolUseFailure, `error` becomes the output and `is_error` is True.
  - On Stop, `assistant_text` ← `last_assistant_message`.
  - `ToolSearch` and `SubagentHandback` give `None`.
- [ ] **Step 2: Render tests,** separately for success and failure post-tool.
  - `context` and `tool_note` are emitted as `{"hookSpecificOutput": {"hookEventName": <reverse name>, "additionalContext": text}}`, joined by a blank line.
  - `notice` is emitted as the top-level `"systemMessage"`.
  - A block becomes `permissionDecision: "deny"` with the reason, exit 0.
  - Turn-end, session-end and agent events emit nothing.
  - `emitted` names exactly the fields written.
- [ ] **Step 3: Implement. Commit** `feat(hook): Claude Code adapter`.

### Task 5: Codex and Vibe adapters

**Files:**
- Create: `src/compchem_memory/hook/adapters/codex.py`, `hook/adapters/vibe.py`
- Test: `tests/hook/test_adapter_codex.py`, `tests/hook/test_adapter_vibe.py`

- [ ] **Step 1: Codex.**
  - Replay every hook fixture under `codex/`. Skip `parent-events` and `mcp-events-no-hooks`, which are `--json` streams.
  - The fields are Claude's, with `turn_id` taken from the payload.
  - Rendering uses Claude's `additionalContext` and `permissionDecision` shapes (`scripts/probes/log_hook.py:34-42`).
  - `notice` is joined into `additionalContext` with the prefix `[Magnolia notice] `, and `emitted` includes it.
- [ ] **Step 2: Vibe.**
  - Event map: `session_start` → `session-start`, `pre_agent` → `prompt`, `pre_tool` → `pre-tool`, `post_tool` → `post-tool`, `post_agent` → `turn-end`, `session_end` → `session-end`. `notification` gives `None`.
  - Fields: `tool_name`, `tool_call_id`, `tool_input`, and `tool_output_text` (falling back to `tool_output`). `tool_status == "failure"` sets `is_error`, with `tool_error` as the output. `turn_id` is taken where present.
  - A child event (`parent_session_id` set) gets `session_id = parent_session_id` and `agent = AgentRef(<child session_id>, agent_name)`.
  - Context is emitted as `{"hook_specific_output": {"additional_context": text}}` on `session_start`, `pre_agent` and `post_tool`, with notices joined in using the same prefix.
  - A deny is `{"decision": "deny", "reason": reason}`.
  - Replay every fixture under `vibe/`.
- [ ] **Step 3: Implement. Commit** `feat(hook): Codex and Vibe adapters`.

### Task 6: `compchem_memory.harness`: neutral description, launch protocol, merge-preserving writers

**Files:**
- Create: `src/compchem_memory/harness/__init__.py`, `harness/neutral.py`, `harness/files.py`, `harness/__main__.py`
- Test: `tests/harness/test_neutral.py`, `tests/harness/test_files.py`, `tests/harness/test_launch_cli.py`

**Interfaces:**
- `neutral.load(template: Path, subs: dict[str, str], setup: dict) -> NeutralConfig`.
  - `NeutralConfig(servers: list[McpServer], deny_tools: frozenset[str])`.
  - `McpServer(name, kind: "local"|"remote", command: list[str] | None, url: str | None, env: dict[str, str], timeout_ms: int, enabled: bool)`.
  - Placeholders and the memory-model injection follow `softwares/bin/magnolia:371-393`. `tools.bash: false` gives `deny_tools={"bash"}`.
- `files.merge_json(path, owned: Callable[[key, value], bool], magnolia: dict)` and `files.merge_toml(path, …)` keep every entry that `owned` rejects. Writes are atomic.
  - TOML is read with `tomllib` and written by a small writer in `files.py` that covers tables, arrays of tables, strings, ints, bools and string lists. No new dependency.
- Launch protocol: `python -m compchem_memory.harness launch --harness H --root <opencode_cc_mem> --project <name> --python <venv python> --tools-port N [--model M]`.
  - On success it writes the files and exits 0. Stdout carries each argv item terminated by NUL, then one empty item (a second NUL), then each `KEY=VALUE` env item terminated by NUL.
  - When the client binary is missing it exits 2 with one line on stderr.
  - Any other failure exits 1 with one line on stderr. The launcher passes the code on and prints the line.

- [ ] **Step 1: Tests.**
  - `neutral.load`: three servers; `memory_enabled` false sets `compchem-memory` to `enabled: false`.
  - `test_render_preserves_member_entries_and_is_idempotent`: a member server and a member hook survive, and rendering twice gives byte-equal files.
  - `test_render_quotes_awkward_paths`: a project named `a "b" é c`, with every file loaded back.
  - The NUL framing, parsed by a reference Bash 3.2 loop in the test.
- [ ] **Step 2: Implement. Commit** `feat(harness): neutral description, launch protocol, merge-preserving writers`.

### Task 7: Per-client renderers

**Files:**
- Create: `src/compchem_memory/harness/render_claude.py`, `render_codex.py`, `render_vibe.py`
- Test: `tests/harness/test_render_claude.py`, `test_render_codex.py`, `test_render_vibe.py`

Every renderer writes `.magnolia/harness/<H>/policy.json` (`{"deny_tools": ["bash"]}`) and returns `MAGNOLIA_HOOK_POLICY` in the launch env.

- [ ] **Step 1: Claude.**
  - Files: `settings.json` (hooks only, every event of Task 4, Stop with `async: true`), `mcp.json` (`mcpServers`; local servers as `command`/`args`/`env`, remote ones as `{"type": "http", "url"}`; disabled servers left out), and `system-prompt.md` (`opencode_cc_mem/AGENTS.md`, then the rule sections that `hook.boot_context` assembles).
  - argv: `claude --setting-sources project,local --settings <settings.json> --mcp-config <mcp.json> --strict-mcp-config --disallowedTools Bash --append-system-prompt-file <system-prompt.md> [--model M]`.
  - env: `MCP_TOOL_TIMEOUT=660000`.
  - Skills: one link per skill, `projects/<p>/.claude/skills/<name>` → `opencode_cc_mem/.opencode/skills/<name>`. A stale link is replaced; a real directory of the same name is left alone and logged.
  - Tests: `test_claude_argv_keeps_project_settings`, `test_memory_disabled_drops_memory_server`, `test_claude_never_uses_bare_or_bypass`, `test_claude_policy_env_present`.
- [ ] **Step 2: Codex.**
  - Files: `.codex/config.toml` (`[mcp_servers.<name>]` with `command`/`args`/`env` or `url`, and `tool_timeout_sec = timeout_ms // 1000`) and `.codex/hooks.json` (the probe's shape, `scripts/probes/run_probes.sh:60-71`; Stop gets `async` only if Codex's bundled docs show the key, and the report says which).
  - argv: `codex -C <project> -s workspace-write [-m M]`.
  - Skills are linked into `projects/<p>/.agents/skills/` when `codex --help` or its bundled docs name that directory. Otherwise the report says Codex gets no skills, and Task 13 checks that statement.
  - Tests: `test_codex_policy_blocks_shell_rendered`, `test_codex_config_has_no_bypass_and_no_codex_home`.
- [ ] **Step 3: Vibe.**
  - `.vibe/config.toml`: `[[mcp_servers]]` with `transport = "stdio"` or `"http"` and `tool_timeout_sec = timeout_ms // 1000` (Vibe's default is 60 s), plus `active_model = M` when a model is given. Also the shell disabled through the config key that `vibe/core/config` defines for disabled tools, if one exists (the report names it). Interactive `vibe` has no `--model` flag (`vibe --help`).
  - `.vibe/hooks.toml`: `[[hooks]]` with names prefixed `magnolia-`, and `strict = true` on `pre_tool`.
  - argv: `vibe --workdir <project>`.
  - Skills: links under `projects/<p>/.agents/skills/`.
  - Tests: `test_vibe_mcp_timeout_is_660`, `test_vibe_pre_tool_hook_is_strict`, `test_vibe_policy_env_present`.
- [ ] **Step 4: Commit** `feat(harness): render Claude Code, Codex and Vibe configurations`.

### Task 8: Session-start content per client

**Files:**
- Modify: `hook/dispatch.py` (`_session_start`), `hook/boot_context.py`
- Test: `tests/hook/test_dispatch.py`

- [ ] **Step 1: Tests.**
  - Claude: the context holds the directive and at most `CLAUDE_CONTEXT_LIMIT = 1800` characters of the dynamic part (boot context and audit report). Past the limit it ends with this pointer, verbatim: `[Magnolia] The full project memory summary is in <abs path>/.magnolia/boot-context.md and audit-report.md: read both before your first task.`
  - Codex: the full payload. Codex loads `AGENTS.md` itself from the repository root down.
  - Vibe: the full payload with `AGENTS.md` first (`build_boot_payload(paths, include_agents_md=True)`).
  - The OpenCode-parity tests stay green.
- [ ] **Step 2: Implement. Commit** `feat(hook): session-start context sized per client`.

### Task 9: Launcher `--harness` and setup

**Files:**
- Modify: `opencode_cc_mem/softwares/bin/magnolia`
- Test: `opencode_cc_mem/tests/test_launcher_harness.py`

**Interfaces:**
- `magnolia [--harness opencode|claude|codex|vibe] [--profile P] <project>`. The flag beats the `harness` key in `llm-setup.json`, which beats the default, `opencode`.
- New helpers: `setup_get_json <dotted key>` and `setup_set_json <dotted key> <json value>`. They are atomic, and they keep every other key and every existing key's value.

- [ ] **Step 1: Tests.**
  - `test_harness_flag_beats_setup_key`, `test_setup_key_selects_harness`, `test_unknown_harness_is_an_error`.
  - `test_missing_client_binary_exits_before_daemons`.
  - `test_claude_launch_without_any_opencode_binary`: neither OpenCode stub is on `PATH`.
  - `test_claude_launch_argv`: an absolute `MAGNOLIA_PROJECT_DIR`, cwd `projects/<p>`, and `MAGNOLIA_HOOK_POLICY` set.
  - `test_dry_run_hides_sentinel_credentials`: `MAGNOLIA_LLM_API_KEY` and `MAGNOLIA_CRITIC_API_KEY` set to sentinels that must not appear.
  - `test_setup_set_json_keeps_other_keys`.
  - The Task 1 snapshot tests stay green.
- [ ] **Step 2: Implement.**
  - The harness is resolved right after flag parsing.
  - The OpenCode-only steps (v2 binary check l.709-712, the binary on `PATH` check l.740-743, the version call l.866) run only when the harness is `opencode`.
  - The client binary check for other harnesses comes before the daemons.
  - Shared steps run unchanged. `render_config` still runs, because the daemon and campaigns read `opencode.json`.
  - For a non-OpenCode harness the launcher calls the Task 6 launch protocol, reads argv and env with `while IFS= read -r -d '' a`, exports the env, `cd`s into `projects/<p>` and runs `exec "${ARGV[@]}"`.
- [ ] **Step 3: Setup.** `magnolia setup --harness H [--model M] <project>`:
  - checks the client with one call: `claude -p 'Reply with exactly: MAIN_MODEL_OK' [--model M]`, `codex exec [-m M] '…'`, or `VIBE_DISABLE_TELEMETRY=true vibe -p '…' [--model M]`;
  - writes `harness`, and `models.<H>` only after the check passes;
  - prints one line naming the client whose subscription the check used.

  Messages in the non-OpenCode branch name the client rather than OpenCode. Commit `feat(launcher): magnolia --harness claude|codex|vibe`.

### Task 10: Client-neutral rules prose

**Files:**
- Modify: `opencode_cc_mem/AGENTS.md` (l.3, 108, 112, 132-133), `rules/magnolia.md` (l.97), `rules/inbox.md` (l.11), `rules/memory-setup.md` (l.66)
- Test: `opencode_cc_mem/tests/test_rules_prose.py`

- [ ] **Step 1: Test.** No always-loaded file says "Opencode's bash tool", "Restart opencode" or "loaded by opencode", and `AGENTS.md` holds `## Tool names in other clients`.
- [ ] **Step 2: Rewrite.**
  - "Opencode's bash tool is disabled" → "The client's built-in shell is disabled".
  - "opencode session" → "agent session".
  - "Restart opencode" → "Restart the agent client".
  - The new section, verbatim: `These rules spell tools the OpenCode way (compchem-tools_run_shell). In Claude Code and Codex the same tool is mcp__compchem-tools__run_shell; in Mistral Vibe it is mcp_compchem-tools.run_shell. Skills are loaded with the skill tool in OpenCode and are listed as skills in the other clients.`
  - The `compchem-tools_*` examples stay.
- [ ] **Step 3: Commit** `docs(rules): client-neutral wording for the shell, sessions and restarts`.

### Task 11: Session identity and export contract

**Files:**
- Create: `src/compchem_memory/transcript_source.py`
- Test: `tests/test_transcript_source.py`

**Interfaces:**
- `SessionRef(harness: str, session_id: str, key: str, first_ts: str | None, last_ts: str | None, sealable: bool)`.
  - `key` is `session_id` for OpenCode, so legacy cursor files, provenance values and `opencode-distilled/<sid>.json` names stay as they are. For other clients it is `f"{harness}-{safe_id(session_id)}"`, using Plan 1's file-name sanitiser.
  - `first_ts` comes from the mapping row `ts`. `last_ts` is the transcript file's mtime for hook-fed sessions, and the last mapping row for OpenCode.
  - `sealable` is True only for OpenCode sessions. A hook-fed transcript is a local file, cheap to reread; it is never sealed, because a client can resume a session and append to it.
- `list_sessions(store) -> list[SessionRef]`:
  - A legacy-only store returns exactly `_read_mapping_ids`' order.
  - A mixed store returns legacy sessions in that order, then hook-fed sessions by `first_ts`.
- `active_opencode_sid(store) -> str | None` is exactly today's `_latest_sid` over the legacy rows.
- `exporter_for(store, injected: Callable[[str], dict | None] | None = None) -> Callable[[SessionRef], dict | None]`:
  - OpenCode sessions go to `injected` or `plugin_dump_exporter`, keeping the DB → dump → CLI order.
  - Other sessions go to `magnolia_exporter(store)`.
- `magnolia_exporter` converts transcript rows into `{"messages": [{"info": {"id", "role", "agent"}, "parts": [...]}]}`.
  - Ids are `f"{key}:{row index}"`, stable because the file is append-only.
  - `agent` and the tool server stay in the export.
  - Prompts become user text, `assistant` rows assistant text, and post-phase tool rows tool parts with state `completed` or `error`.
- `resolve_transcript(store, key) -> Path | None` is the reverse lookup that `dreaming` uses.
  - A legacy key resolves as today: `opencode-transcripts/<sid>.json`, then `sessions/<sid>.jsonl`.
  - A `<harness>-…` key resolves to `transcripts/<harness>/<sid>.jsonl`.

- [ ] **Step 1: Tests.**
  - `test_legacy_only_order_and_active_unchanged`.
  - `test_mixed_store_lists_both`.
  - `test_magnolia_exporter_reconstructs_like_opencode`: `reconstruct_transcript` on an exported Claude transcript keeps roles, text and tool order.
  - `test_injected_string_exporter_still_used_for_opencode`.
  - `test_resolve_transcript_both_kinds`.
  - `test_collegium_materialised_transcript_regression`, with a fixture built from the shape in `collegium@e64e411:src/collegium/worker.py:105-129` and a comment naming that source.
- [ ] **Step 2: Implement. Commit** `feat(memory): one session identity for every client`.

### Task 12: Distillation, handover and surfacing use the contract

**Files:**
- Modify: `opencode_ingest.py` (l.346, 379, 484-560), `handover.py` (l.612, 894-1006), `startup_scan.py` (l.20-61), `server.py` (l.945-960), `surfacing.py` (l.50-53), `dreaming.py` (l.235)
- Test: additions to `tests/test_opencode_ingest.py`, `tests/test_handover.py` and `tests/test_startup_scan_dispatch.py`. Existing tests stay unchanged and green.

- [ ] **Step 1: Tests.**
  - `test_switching_clients_never_seals_an_active_session`: OpenCode, then Claude, then OpenCode, with a late append to the Claude transcript after a sweep.
  - `test_legacy_only_store_unchanged`: same candidates, same progress files, same seals as at 6bac3aa.
  - `test_two_harnesses_distilled_without_cursor_collision`.
  - `test_provenance_uses_key`: staged candidates from a Claude session carry `opencode_session_id = key` and `observed_in_sessions` containing `key`.
  - `test_startup_scan_dialogue_for_hook_sessions_without_opencode`.
  - `test_handover_reads_hook_sessions`: timestamps from `SessionRef`, and update lookups by raw id for OpenCode only.
- [ ] **Step 2: Implement.**
  - Ingestion iterates `list_sessions`. It seals only `sealable` sessions that are not `active_opencode_sid`.
  - `startup_scan` enters dialogue mode when there is a usable source: a hook-fed session with a transcript, or an OpenCode mapping with `_opencode_available()`. Otherwise it keeps today's tool-event fallback, so the protected tests in `tests/test_startup_scan_dispatch.py` still pass.
  - Handover, the server's manual distil tool, surfacing (`guess_session_id`) and dreaming go through the contract.
- [ ] **Step 3: Run the full suite. Commit** `feat(memory): distil and hand over sessions from every client`.

### Task 13: Live acceptance per client, and the docs

**Files:**
- Create: `opencode_cc_mem/docs/harness-acceptance-2026-11.md`
- Modify: `opencode_cc_mem/docs/harness-neutral-agents-design.md` (section 9 status; section 5 Vibe critic row), `docs/harness-adaptation.md` (a "Running Magnolia in Claude Code, Codex or Vibe" section), `docs/getting-started.md` (`--harness`, and trusting a project once)

- [ ] **Step 1: Demo workspace under `$TMPDIR`:** a copy of the launcher inputs with one project and one entry that matches a `run_shell` command. Add a member-owned hook and MCP server in each client's project configuration to check coexistence. The tools daemon runs on its usual port.
- [ ] **Step 2: Replay the dry-run launch exactly:** argv, cwd and env from `MAGNOLIA_DRY_RUN=1 magnolia --harness H demo`. Add the non-interactive flags each client needs:
  - Claude: `-p "<task>" --allowedTools mcp__compchem-tools__run_shell`;
  - Codex: `exec`;
  - Vibe: `-p … --trust --auto-approve` with `VIBE_DISABLE_TELEMETRY=true`, on the temp project only.

  The Codex hooks of the demo project need LFX's choice at execution time: trust them once in Codex, or use the bypass flag on this temp project only.
- [ ] **Step 3: Check per client, at most five calls each.** Record each check as seen, not seen or unknown, with the evidence:
  - boot context: the model's reply names the project goal;
  - an action note: an `action-retrieval.jsonl` row with `injected > 0` plus its `surfacing.jsonl` rows. The hook transcript is written before retrieval, so the note is not in it;
  - the `agent-sessions.jsonl` row and the transcript file, with the recovered reply text (Task 3);
  - a shell call refused by the policy;
  - the member's own hook and server still working;
  - skills listed by the client, or the documented absence for Codex;
  - a critic row. This needs a critic key and LFX's go; without them it is recorded as not done.
- [ ] **Step 4: Distillation.** Run `scan_and_distill` on the demo store with a real memory model, which needs LFX's go for one call per client. A stubbed distiller does not count as acceptance: without the go, the phase is reported as "not accepted: distillation not run live". OpenCode is not installed on this Mac, so the comparison with an OpenCode session uses `tests/fixtures/v2_session_export.json`, and the report says so.
- [ ] **Step 5: Report and docs.** Remove the demo workspace. Commit `docs: harness acceptance for Claude Code, Codex and Vibe`.
