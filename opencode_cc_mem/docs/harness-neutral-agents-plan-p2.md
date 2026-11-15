# Harness-neutral Magnolia — Plan 2: client adapters and `magnolia --harness`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `magnolia --harness claude|codex|vibe <project>` opens an interactive session in that client, signed in with the member's own login. Magnolia's MCP servers, rules, boot context and hooks are present, and the sessions are distilled like OpenCode's. `magnolia <project>` without the flag keeps doing exactly what it does today.

**Architecture:** Three adapters translate each client's hook payloads to and from the neutral `HookEvent`/`HookResponse` of Plan 1. A Python package `compchem_memory.harness` reads the MCP servers from `opencode.json.template`, the one existing description, and renders each client's configuration. It prints the client command line, which the Bash launcher executes. Distillation and handover read sessions through `hook.sessions.iter_sessions`. A transcript exporter makes Magnolia's own transcripts look like an OpenCode export, so the existing distiller runs unchanged.

**Tech Stack:** Python 3.11+ (`compchem-memory`), pytest, Bash 3.2 (macOS `/bin/bash`), Claude Code 2.1.325, codex-cli 0.154.0, Mistral Vibe 3.1.2.

**Spec:** `opencode_cc_mem/docs/harness-neutral-agents-design.md` (approved by LFX on 15 Nov 2026), sections 5, 6.3 and 7, and phases P2, P3 and the interactive half of P5. The evidence is `opencode_cc_mem/docs/harness-probes-2026-11.md` and the fixtures in `opencode_cc_mem/mcp-servers/compchem-memory/tests/fixtures/hooks/{claude,codex,vibe}/`. The facts about the code base come from `.superpowers/sdd/harness-neutral-agents-plan-p2/inventory.md` (git-ignored; read 15 Nov 2026).

**Deferred to Plan 3:** routing (`routes` in `llm-setup.json`), the campaign, reviewer, helper-agent and evaluator executors, the usage governor, and campaign path rules enforced by `pre-tool` blocks. The agents daemon and `magnolia-exp` keep rendering `opencode.json` themselves until then.

## Global Constraints

- **Paths:** `src/…` and `tests/…` are relative to `opencode_cc_mem/mcp-servers/compchem-memory/`. Other paths are relative to the repository root.
- **The OpenCode path does not change.** `magnolia [--profile P] <project>` with no `--harness`, and no `harness` key in `llm-setup.json`, renders a byte-identical `projects/<p>/opencode.json` and executes the same command, in the same directory, with the same environment. Task 6 adds a characterisation test of this before it touches the launcher. Nothing in `.opencode/plugins/` changes (design G5, A4).
- **Credentials (design A1).** Magnolia never reads, copies or moves a client's login: no `CLAUDE_CONFIG_DIR`, no `CODEX_HOME`, no `VIBE_HOME` override, no copy of `auth.json` or the keychain. Never use `claude --bare`. Never put `--dangerously-bypass-hook-trust`, `--dangerously-skip-permissions`, `--auto-approve` or `--trust` in any rendered configuration or interactive command line. The member trusts a project once, through the client's own prompt or command.
- **The CLI fails open** (Plan 1): it always exits 0 and prints nothing on error, except for a deliberate block. An event the adapter does not handle prints nothing and writes no transcript row.
- **Names Collegium reads stay as they are** (plan 1, "Alignment with Collegium"). That includes `opencode-sessions.jsonl`, its key `opencode_session_id`, `opencode-transcripts/` and `opencode-distilled/`. New files sit beside them.
- **Bash 3.2:** the launcher uses no `mapfile`, no `declare -A`, no `${var,,}`. It reads NUL-separated argv with `while IFS= read -r -d '' a; do ARGV+=("$a"); done`.
- **Rendered files** go under `projects/<p>/` and nowhere else: `.magnolia/harness/<H>/`, plus `.codex/` and `.vibe/` where the client reads only those, plus `.claude/skills/` and `.agents/skills/` links. They are rewritten on every launch, atomically (temporary file, then replace). Nothing under `~/.claude`, `~/.codex` or `~/.vibe` is written.
- **Hook commands** are `<absolute venv python> -m compchem_memory.hook <harness> <event>`. Timeouts: 10 s for post-tool and 30 s for the rest, set in the client's hook configuration. Turn-end runs asynchronously wherever the client allows it (Claude `async: true`).
- **Live runs:** at most ten short calls per client on the member's subscription. Vibe runs set `VIBE_DISABLE_TELEMETRY=true`. Each live run uses a demo project created under `$TMPDIR`; the member's own projects are never used. Codex hook trust for the demo project needs LFX's go at execution time.
- **Tests** run from `opencode_cc_mem/mcp-servers/compchem-memory` with `python -m pytest -q tests`. Launcher tests live in `opencode_cc_mem/tests/test_launcher_harness.py`, run with `python -m pytest -q opencode_cc_mem/tests/test_launcher_harness.py` from the repository root, with stub client binaries on `PATH`. The baseline is 1081 passed, 4 skipped (6bac3aa); each task records the counts it observed.

## Review Focus

1. **The member's own project hooks and settings** (a `.claude/settings.json` that another tool placed in the project) still run beside Magnolia's. Test: Task 4 `test_claude_argv_keeps_project_settings` checks for `--setting-sources project,local`, and for `--settings` pointing at a file that holds only Magnolia's hooks. Live check in Task 9.
2. **A client that is not installed, or not on `PATH`:** the launcher says which binary is missing and exits 1 before it starts any daemon. Test: Task 6 `test_missing_client_binary_exits_before_daemons`.
3. **A project path with spaces, quotes or non-ASCII characters** in the rendered JSON and TOML. Test: Task 4 `test_render_quotes_awkward_paths` loads every rendered file back with `json`/`tomllib`.
4. **Memory disabled** (`memory_enabled: false`): the `compchem-memory` server is left out of every client's configuration, and the hooks still run. Test: Task 4 `test_memory_disabled_drops_memory_server`.
5. **One project used by two clients in turn:** sessions from both are distilled, cursors do not collide, and legacy OpenCode rows are still read. Test: Task 8 `test_two_harnesses_distilled_without_cursor_collision`.
6. **Parallel hooks of one session** (Claude runs PostToolUse hooks in parallel): a job notice or critic flag is shown once. Test: Task 1 `test_parallel_events_show_a_notice_once`.

---

### Task 1: Core changes the adapters need

**Files:**
- Modify: `src/compchem_memory/hook/events.py` (`canonical_tool`), `hook/adapters/__init__.py` (`Adapter.parse` may return `None`), `hook/__main__.py`, `hook/dispatch.py` (`pre-tool` policy), `hook/notices.py` (reservation)
- Create: `src/compchem_memory/hook/policy.py`
- Test: `tests/hook/test_events.py`, `tests/hook/test_policy.py`, `tests/hook/test_notices.py`, `tests/hook/test_cli.py`

**Interfaces:**
- Produces: `canonical_tool` handles the Vibe forms. `Adapter.parse(raw, event) -> HookEvent | None`, where `None` means "ignore this event". `policy.load(env) -> Policy` and `policy.decide(event, policy) -> Block | None`. `Handled` for `pre-tool` carries the block.

- [ ] **Step 1: Tests for the tool names.** Add `test_canonical_tool_vibe_forms`: `mcp_compchem-tools.run_shell` → `("compchem-tools", "run_shell")`; `file_system.bash` → `(None, "bash")`; `file_system.write_file` → `(None, "write")`; `file_system.edit` → `(None, "edit")`; `agents.spawn` → `(None, "agents.spawn")`. Existing cases unchanged. `Bash` (Claude and Codex) → `(None, "bash")`, so `ACTION_TOOLS` and the policy match every client's shell under the one name `bash`.
- [ ] **Step 2: Implement them in `canonical_tool`.** The `mcp_` prefix is checked after `mcp__`, splitting on the first `.`. Built-in `file_system.<x>` maps `write_file` to `write` and keeps the other names.
- [ ] **Step 3: Test and implement the shell policy.** `Policy(deny_tools: frozenset[str], reason: str)` is read from the JSON file named by `MAGNOLIA_HOOK_POLICY`; with no file, nothing is denied. On `pre-tool`, `dispatch` returns `HookResponse(block=Block(reason))` when the canonical name is in `deny_tools`. The reason text, verbatim: `Magnolia disables this client's built-in shell. Use the compchem-tools run_shell tool instead; it records the command for the project's memory.` Tests: `test_policy_blocks_bash_on_pre_tool`, `test_no_policy_file_blocks_nothing`, `test_policy_ignores_post_tool`.
- [ ] **Step 4: Ignored events.** `__main__` treats `parse(...) is None` as "exit 0, print nothing, write nothing". Test: `test_ignored_event_writes_no_row` with a stub adapter.
- [ ] **Step 5: Notices are reserved like action notes (deferred minor from Plan 1).** `pending_notices` moves the notices it returns to an `in_flight` list with a token, under the state lock; `commit_notices` drops them, and a new `release_notices` puts them back. The CLI calls `release_notices` when `notice` was not emitted, and on failure. Test: `test_parallel_events_show_a_notice_once` interleaves two selections before either commit.
- [ ] **Step 6: Isolate commit failures (deferred minor from Plan 1).** A failure inside `_commit_delivered` is logged and keeps the rendered exit code (a block stays exit 2). Test: `test_commit_failure_keeps_block_exit_code`.
- [ ] **Step 7: Run `tests/hook` and the full suite, then commit** `feat(hook): Vibe tool names, shell policy, ignored events, reserved notices`.

### Task 2: Claude Code adapter

**Files:**
- Create: `src/compchem_memory/hook/adapters/claude.py`
- Modify: `hook/adapters/__init__.py` (register `"claude"`)
- Test: `tests/hook/test_adapter_claude.py`

**Interfaces:**
- Consumes: Task 1. The fixtures in `tests/fixtures/hooks/claude/`.
- Produces: `ADAPTERS["claude"]`. The client event names the adapter accepts as the CLI's `<event>` argument are the neutral names. The rendered settings (Task 4) map Claude events to them as follows: SessionStart→`session-start`, UserPromptSubmit→`prompt`, PreToolUse→`pre-tool`, PostToolUse and PostToolUseFailure→`post-tool`, Stop→`turn-end`, SubagentStart→`agent-start`, SubagentStop→`agent-end`, SessionEnd→`session-end`.

- [ ] **Step 1: Replay tests on every fixture under `claude/`.** Each payload parses to a `HookEvent` whose `session_id`, `cwd`, `transcript_ref` (`transcript_path`), `turn_id` (`prompt_id`) and `agent` (`agent_id`/`agent_type`, null on parent events) match the payload. `tool_name` gives the raw name. `tool_use_id` is the call id, `tool_input` the input and `tool_response` the output, passed through `normalise_output`. On PostToolUseFailure `error` becomes the output and `is_error` is True. On Stop, `last_assistant_message` becomes `assistant_text`. `ToolSearch` and `SubagentHandback` tool events parse to `None`.
- [ ] **Step 2: Render tests.** For `session-start`, `prompt` and `post-tool`, `context` and `tool_note` go out as `{"hookSpecificOutput": {"hookEventName": <native name>, "additionalContext": text}}`, the two joined by a blank line when both are present. `notice` goes out as top-level `"systemMessage"`. On `pre-tool` a block is `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}` with exit 0. Turn-end, session-end and agent events emit nothing. `emitted` names exactly the fields written. The native event name comes from the payload's `hook_event_name`.
- [ ] **Step 3: Implement `parse` and `render`. Run the tests, then commit** `feat(hook): Claude Code adapter`.

### Task 3: Codex and Vibe adapters

**Files:**
- Create: `src/compchem_memory/hook/adapters/codex.py`, `hook/adapters/vibe.py`
- Modify: `hook/adapters/__init__.py`
- Test: `tests/hook/test_adapter_codex.py`, `tests/hook/test_adapter_vibe.py`

**Interfaces:**
- Produces: `ADAPTERS["codex"]` and `ADAPTERS["vibe"]`. Codex event map: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, as for Claude. Vibe event map: `session_start`→`session-start`, `pre_agent`→`prompt`, `pre_tool`→`pre-tool`, `post_tool`→`post-tool`, `post_agent`→`turn-end`, `session_end`→`session-end`; `notification` → `None`.

- [ ] **Step 1: Codex replay and render tests** on every fixture under `codex/` that holds hook payloads (the `*-events` and `mcp-events-no-hooks` scenarios are `--json` streams and are skipped). Codex sends the Claude field names, `turn_id` included, and no `mcp_server`. Rendering matches Claude's shape for `additionalContext` and `permissionDecision`; see `scripts/probes/log_hook.py:34-42`. Codex has no `systemMessage` that P0 observed, so `notice` is appended to `additionalContext` with the prefix `[Magnolia notice] `. `emitted` then includes `notice`.
- [ ] **Step 2: Vibe replay and render tests** on every fixture under `vibe/`. The fields are `tool_name`, `tool_call_id`, `tool_input`, `tool_output_text` (falling back to `tool_output`), and `tool_status == "failure"` → `is_error`, with `tool_error` as the output. A child event (`parent_session_id` set) gets `session_id = parent_session_id` and `agent = AgentRef(id=<child session_id>, type=agent_name)`, so it lands in the parent's transcript, as on Claude and Codex. Context goes out as `{"hook_specific_output": {"additional_context": text}}` on `session_start`, `pre_agent` and `post_tool`. Notices are joined into that context with the same prefix. A deny is `{"decision": "deny", "reason": reason}`. `post_agent` carries no assistant text, so `assistant_text` stays None (design 5, degraded form).
- [ ] **Step 3: Implement both adapters. Run the tests, then commit** `feat(hook): Codex and Vibe adapters`.

### Task 4: `compchem_memory.harness`: neutral description and per-client rendering

**Files:**
- Create: `src/compchem_memory/harness/__init__.py`, `harness/neutral.py`, `harness/render_claude.py`, `harness/render_codex.py`, `harness/render_vibe.py`, `harness/__main__.py`
- Test: `tests/harness/test_neutral.py`, `tests/harness/test_render.py`, `tests/harness/test_launch_cli.py`

**Interfaces:**
- Consumes: `opencode_cc_mem/opencode.json.template`, `llm-setup.json`, `hook.boot_context.build_boot_payload`, the skills under `.opencode/skills/`.
- Produces:
  - `neutral.load(template: Path, subs: dict[str, str]) -> NeutralConfig`, with `NeutralConfig(servers: list[McpServer], deny_tools: frozenset[str])` and `McpServer(name, kind: "local"|"remote", command: list[str] | None, url: str | None, env: dict[str, str], timeout_ms: int, enabled: bool)`. The placeholders and the memory-model injection are the same as in `render_config`, `softwares/bin/magnolia:371-393`. `tools.bash: false` becomes `deny_tools={"bash"}`.
  - `render(harness, cfg, ctx) -> Launch`, where `Launch(argv: list[str], env: dict[str, str], files: list[Path])`.
  - The CLI: `python -m compchem_memory.harness launch --harness H --root <opencode_cc_mem> --project <name> --python <venv python> --tools-port N [--model M]`. It writes the files and prints `argv` NUL-separated on stdout, then `--env` lines `KEY=VALUE` after a lone NUL. It exits 2 with a one-line message when the client binary is not on `PATH`.

- [ ] **Step 1: `neutral.load` tests.** Three servers come from the template. `compchem-memory` is a local server with the memory environment, `compchem-tools` is remote on the given port, and `perspicacite` is remote. With `memory_enabled` false, `compchem-memory` has `enabled` false. Then implement it.
- [ ] **Step 2: Claude rendering.**
  - Files: `projects/<p>/.magnolia/harness/claude/settings.json` (hooks only, events mapped as in Task 2, Stop with `async: true`), `mcp.json` (`mcpServers`; local servers as `command`/`args`/`env`, remote ones as `{"type": "http", "url": ...}`; disabled servers left out), `policy.json` (`{"deny_tools": ["bash"]}`), and `system-prompt.md` (`opencode_cc_mem/AGENTS.md` followed by the rule sections that `hook.boot_context` assembles; the project's boot context and audit report are left out, since they reach Claude through the session-start hook, see Task 5).
  - argv: `claude --setting-sources project,local --settings <settings.json> --mcp-config <mcp.json> --strict-mcp-config --disallowedTools Bash --append-system-prompt-file <system-prompt.md> [--model M]`.
  - env: `MAGNOLIA_HOOK_POLICY=<policy.json>`, `MCP_TOOL_TIMEOUT=660000`.
  - Skills: one symlink per skill, `projects/<p>/.claude/skills/<name>` → `opencode_cc_mem/.opencode/skills/<name>`. A stale link is replaced; a real directory with the same name is left alone and logged.
  - Tests: `test_claude_argv_keeps_project_settings`, `test_render_quotes_awkward_paths`, `test_memory_disabled_drops_memory_server`, `test_claude_never_uses_bare_or_bypass`.
- [ ] **Step 3: Codex rendering.**
  - Files: `projects/<p>/.codex/config.toml` (`[mcp_servers.<name>]` with `command`/`args`/`env`, or `url`, and `tool_timeout_sec = 660`) and `.codex/hooks.json` (the probe's shape, `scripts/probes/run_probes.sh:60-71`).
  - argv: `codex -C <project> -s workspace-write [-m M]`.
  - Skills are linked into `projects/<p>/.agents/skills/` only if Codex's own documentation (`codex --help`, or the bundled docs) names that directory. Record what was found in the report.
  - Test: `test_codex_config_has_no_bypass_and_no_codex_home`.
  - Before the first launch of a project, the launcher prints one line once: the hooks must be trusted in Codex (`/hooks`).
- [ ] **Step 4: Vibe rendering.**
  - Files: `projects/<p>/.vibe/config.toml` (`[[mcp_servers]]`, `transport = "stdio"` or `"http"`) and `.vibe/hooks.toml` (`[[hooks]]` with `name`, `type`, `command`, `timeout`; `strict = true` on `pre_tool` only, so a failing policy hook blocks the tool rather than letting the shell through). Skills are linked into `projects/<p>/.agents/skills/`.
  - argv: `vibe --workdir <project> [--model M]`, and `--disabled-tools file_system.bash` if `vibe --help` lists the flag for interactive use; otherwise the matching key in `config.toml`. Record which one in the report.
  - env: `VIBE_DISABLE_TELEMETRY` is not set by Magnolia for interactive use; that is the member's choice.
  - Test: `test_vibe_pre_tool_hook_is_strict`.
- [ ] **Step 5: `launch` CLI test.** Run `python -m compchem_memory.harness launch` in a subprocess with stub binaries on `PATH`, parse the NUL-separated output, and check the files exist and every file loads. Also test exit 2 for a missing binary. Then commit `feat(harness): render Claude Code, Codex and Vibe configurations from the OpenCode template`.

### Task 5: Session-start content per client

**Files:**
- Modify: `hook/dispatch.py` (`_session_start`), `hook/boot_context.py`
- Test: `tests/hook/test_dispatch.py`

**Interfaces:**
- Produces: `build_boot_payload(paths, *, include_agents_md: bool = False, limit: int = MAX_PAYLOAD_CHARS)`. OpenCode parity is unchanged with the defaults.

- [ ] **Step 1: Tests for the per-client session-start context.**
  - Claude: the context holds the directive, and no more than `CLAUDE_CONTEXT_LIMIT = 1800` characters of the boot payload's dynamic part (boot-context and audit report). If it is longer, the context ends with this pointer, verbatim: `[Magnolia] The full project memory summary is in <abs path>/.magnolia/boot-context.md and audit-report.md: read both before your first task.` The rules and `AGENTS.md` reach Claude through `--append-system-prompt-file` (Task 4), not through the hook (P0: about 2 KB of a 256 KB context reached the model).
  - Codex: the full payload, without `AGENTS.md`, which Codex loads itself from the repository root down to the project.
  - Vibe: the full payload with `include_agents_md=True`.
  - The OpenCode-parity tests stay green.
- [ ] **Step 2: Implement.** The limit is chosen per harness inside `_session_start`. Commit `feat(hook): session-start context sized per client`.

### Task 6: Launcher `--harness` and setup

**Files:**
- Modify: `opencode_cc_mem/softwares/bin/magnolia` (flag loop near l.646-672, a launch branch before l.918-921, `usage()`, `setup_run`)
- Create: `opencode_cc_mem/tests/test_launcher_harness.py`, `opencode_cc_mem/tests/stubs/{opencode,claude,codex,vibe}` (each prints its argv and cwd as JSON, then exits 0)

**Interfaces:**
- Consumes: Task 4's `launch` CLI.
- Produces: `magnolia [--harness opencode|claude|codex|vibe] [--profile P] <project>`. `MAGNOLIA_DRY_RUN=1` prints the final argv, cwd and the `MAGNOLIA_*` environment as JSON instead of executing, and starts no daemon. Two new `llm-setup.json` keys: `harness` (string) and `models` (`{"claude": ..., "codex": ..., "vibe": ...}`, optional), the latter read with a new `setup_get_json <dotted key>` helper.

- [ ] **Step 1: Characterisation test first, on the unchanged launcher.** `test_opencode_launch_unchanged`: with `MAGNOLIA_DRY_RUN` honoured, the argv is `[<opencode bin>]`, the cwd is `projects/<p>` and the env is as today. Then snapshot the rendered `opencode.json`. Adding the dry-run switch is the only launcher change in this step. The snapshot must not change for the rest of the plan.
- [ ] **Step 2: Tests for the new flag.** `test_harness_flag_beats_setup_key`, `test_setup_key_selects_harness`, `test_unknown_harness_is_an_error`, `test_missing_client_binary_exits_before_daemons`, and `test_claude_launch_argv` (checks `MAGNOLIA_PROJECT_DIR` is absolute for non-OpenCode clients, and the cwd).
- [ ] **Step 3: Implement.**
  - The flag is parsed in the flag loop.
  - The harness is resolved before the shared steps. The binary check comes before the tools and agents daemons start.
  - For a non-OpenCode harness, the launcher runs the shared steps unchanged (scaffold, markers, venv, profile, tools daemon, agents daemon). It then calls `"$VENV_PY" -m compchem_memory.harness launch ...`, reads the argv, exports the env lines, `cd`s into `projects/<p>` and runs `exec "${ARGV[@]}"`.
  - `render_config` still runs for every harness, because the agents daemon and campaigns read `opencode.json`.
- [ ] **Step 4: Setup.** `magnolia setup --harness H <project>` writes `harness`. For a non-OpenCode client, step 1 checks the client with one call: `claude -p 'Reply with exactly: MAIN_MODEL_OK' [--model M]`, `codex exec 'Reply with exactly: MAIN_MODEL_OK'` or `vibe -p 'Reply with exactly: MAIN_MODEL_OK'` (with `VIBE_DISABLE_TELEMETRY=true`). It then writes `models.<H>` if a model was given. It prints one line saying which subscription the check used. Messages that say "opencode" become client-neutral for the non-OpenCode branch only. Commit `feat(launcher): magnolia --harness claude|codex|vibe`.

### Task 7: Client-neutral rules prose

**Files:**
- Modify: `opencode_cc_mem/AGENTS.md` (l.3, 108, 112, 132-133), `rules/magnolia.md` (l.97), `rules/inbox.md` (l.11), `rules/memory-setup.md` (l.66)
- Test: `opencode_cc_mem/tests/test_rules_prose.py`

- [ ] **Step 1: Test.** No always-loaded file says "Opencode's bash tool", "Restart opencode" or "loaded by opencode". `AGENTS.md` holds a section `## Tool names in other clients`.
- [ ] **Step 2: Rewrite the matched sentences.**
  - "Opencode's bash tool is disabled" → "The client's built-in shell is disabled".
  - "opencode session" → "agent session".
  - "Restart opencode" → "Restart the agent client".
  - Add the section, with these four lines verbatim: `These rules spell tools the OpenCode way (compchem-tools_run_shell).`, `In Claude Code and Codex the same tool is mcp__compchem-tools__run_shell;`, `in Mistral Vibe it is mcp_compchem-tools.run_shell. Skills are loaded with the skill tool in OpenCode`, `and are listed as skills in the other clients.`
  - The `compchem-tools_*` examples stay, because Collegium and OpenCode members read them.
- [ ] **Step 3: Commit** `docs(rules): client-neutral wording for the shell, sessions and restarts`.

### Task 8: Distillation and handover read every client's sessions

**Files:**
- Create: `src/compchem_memory/transcript_source.py`
- Modify: `opencode_ingest.py` (`_read_mapping_ids` l.346, `_latest_sid` l.379, `ingest_opencode_sessions` l.484), `handover.py` (l.932-1006), `startup_scan.py` (l.20-55), `server.py` (l.945-960), `surfacing.py` (l.50-53), `dreaming.py` (l.235)
- Test: `tests/test_transcript_source.py`, plus additions to `tests/test_handover.py` and `tests/test_opencode_ingest.py`

**Interfaces:**
- Consumes: `hook.sessions.iter_sessions(store)`, `hook.transcript.read_turns(path)`, `transcript_path(store, harness, sid)`.
- Produces:
  - `SessionRef(harness: str, session_id: str, key: str)`. `key` is `session_id` for OpenCode, so legacy cursor files keep their names, and `f"{harness}-{session_id}"` for the other clients.
  - `list_sessions(store) -> list[SessionRef]` (from `iter_sessions`, in order).
  - `exporter_for(store) -> Callable[[SessionRef], dict | None]`. It dispatches to `plugin_dump_exporter` for OpenCode, and to `magnolia_exporter(store)` for the others.
  - `magnolia_exporter` converts transcript rows to `{"messages": [{"info": {"id", "role"}, "parts": [...]}]}`. The ids are `f"{key}:{row index}"`, stable across reads because the transcript is append-only. Prompts become user text, `assistant` rows assistant text, and post-phase tool rows tool parts with state `completed`/`error`.

- [ ] **Step 1: Tests.**
  - `test_magnolia_exporter_reconstructs_like_opencode`: `reconstruct_transcript` on the exported Claude transcript fixture keeps the roles, text and tool output order.
  - `test_two_harnesses_distilled_without_cursor_collision`.
  - `test_legacy_opencode_rows_still_distilled`: a store holding only `opencode-sessions.jsonl` gives the same candidates as before.
  - `test_startup_scan_uses_dialogue_mode_for_hook_sessions`: no OpenCode binary, a Claude transcript present, and the LLM stubbed.
  - `test_collegium_materialised_transcript_regression`: the fixture is built from the shape in `collegium@e64e411:src/collegium/worker.py:105-129`, copied into the test with a comment naming its source.
- [ ] **Step 2: Implement.**
  - Replace the five readers of `opencode_session_id` with `list_sessions`, keeping the legacy lookup order.
  - `startup_scan` enters dialogue mode when any session is listed and the LLM is available. The OpenCode exporter is used only for OpenCode sessions, and only when `_opencode_available()`.
  - Progress files keep their directory (`opencode-distilled/`) and are named by `key`.
  - `surfacing.guess_session_id` reads `list_sessions`.
- [ ] **Step 3: Run the full suite, then commit** `feat(memory): distil and hand over sessions from every client`.

### Task 9: Live acceptance per client, and the docs

**Files:**
- Create: `opencode_cc_mem/docs/harness-acceptance-2026-11.md`
- Modify: `opencode_cc_mem/docs/harness-neutral-agents-design.md` (section 9 status), `docs/harness-adaptation.md` (a "Running Magnolia in Claude Code, Codex or Vibe" section), `docs/getting-started.md` (one paragraph on `--harness` and trusting a project once)

- [ ] **Step 1: Demo project.** Under `$TMPDIR`, a workspace copy with one project, one entry that matches a `run_shell` command, and `memory_enabled` false, so no memory-model key is needed. The tools daemon runs on its usual port.
- [ ] **Step 2: Claude, at most five calls.** Run `MAGNOLIA_DRY_RUN=1 magnolia --harness claude demo` to get the argv, then run it with `-p "<task that calls run_shell with the matching command>"`. Record and check: the boot context, seen through the model's reply naming the project goal; the action note in the transcript row and in the reply; the `agent-sessions.jsonl` row; the transcript file; a critic row if a critic key is present, otherwise the logged silent no-op; and a shell call refused by the policy hook. Run one call with a `.claude/settings.json` hook of the demo project's own, and check that both hooks ran.
- [ ] **Step 3: Codex, at most five calls,** with the same checklist, through `codex exec` with the rendered project layer. The hooks of the demo project must be trusted. Ask LFX at execution time whether to trust them in Codex once, or to use `--dangerously-bypass-hook-trust` on this temp demo project only.
- [ ] **Step 4: Vibe, at most five calls,** with the same checklist, through `vibe -p` with `--trust` on the temp demo project only (the P0 rule), `--auto-approve`, and `VIBE_DISABLE_TELEMETRY=true`.
- [ ] **Step 5: Distillation.** Run `scan_and_distill` on the demo store with a memory model if LFX allows one call per client; otherwise run it with a stubbed distiller and say so. OpenCode is not installed on this Mac, so the comparison with an OpenCode session uses the recorded export fixture `tests/fixtures/v2_session_export.json`, and the report says so.
- [ ] **Step 6: Report and docs.** The acceptance report lists every check per client as seen, not seen, or unknown, with the fixture or log line that shows it. Remove the demo project. Commit `docs: harness acceptance for Claude Code, Codex and Vibe`.
