# Harness-neutral Magnolia — Plan 1: client probes (P0) and the hook core (P1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record how Claude Code, Codex and Vibe call hooks around MCP tool calls (P0), then build Magnolia's client-neutral hook core in Python with the behaviours of today's OpenCode plugins (P1), without changing the OpenCode path.

**Architecture:** A package `compchem_memory.hook` receives one normalised `HookEvent`, runs the behaviour for that event (boot context, retrieval directive, doorbell, action retrieval, transcript, session mapping, notices, critic) and returns one `HookResponse`. Each process handles one event; state that the TypeScript plugins kept in memory lives in a per-session JSON file. Client adapters (Claude Code, Codex, Vibe) are Plan 2 onwards; P1 ships only a `json` adapter used by tests.

**Tech Stack:** Python 3.11+ (compchem-memory package), pytest, Node 24 for the parity drivers, Claude Code 2.1.325 and codex-cli 0.154.0 for the probes.

**Spec:** `opencode_cc_mem/docs/harness-neutral-agents-design.md` (approved by LFX, 15 Nov 2026). Sections 5, 6 and 9 (P0, P1) are implemented here; sections 7 and 8 belong to later plans, written after the P0 report.

**Deferred to later plans (written after the P0 report):** client adapters for Claude Code, Codex and Vibe, the `--harness` launcher, rendered client configurations, skills linking and client-neutral rules prose (Plan 2, design P2–P3, P5); distillation and handover reading the Magnolia transcript through a `TranscriptSource` (Plan 2, with the first non-OpenCode sessions); routing, executors and the usage governor (Plan 3, design P4); OpenCode plugins as shims (P6, Tao's call).

## Global Constraints

- Nothing in `.opencode/plugins/` or `softwares/bin/magnolia` changes in this plan (design G5, phase table: "OpenCode plugins untouched").
- Magnolia never reads, copies or sends any client's subscription credentials (design A1). Probes use the clients' own logins as they are; no `--bare`.
- A hook failure never breaks the session: the CLI always exits 0 and prints nothing on error, except a deliberate `block` (design 6.1); errors go to `<store>/hook-errors.jsonl`.
- `HookEvent` and transcript rows carry `schema_version: 1` (design section 11).
- Exact texts and limits copied from the plugins: directive text (`magnolia-auto-retrieval.ts:40-49`), boot payload header and 256 000-character cap (`magnolia-boot-context.ts:24, 119-143`), doorbell letter and replacement text (`magnolia-agent-doorbell.ts:98-140`), action block text, `MIN_SCORE = 3`, `MAX_HITS = 4`, `MAX_INJECTIONS_PER_SESSION = 8`, `MAX_PER_ENTRY_PER_SESSION = 2`, `QUERY_TIMEOUT_MS = 2500`, `CACHE_TTL_MS = 120000` (`magnolia-action-retrieval.ts:63-69, 337-345`), critic caps 2000 / 500 / 80 000 / min report 200 characters (`claim-critic.ts`).
- Kill switches keep their names: `MAGNOLIA_AUTORETRIEVE`, `MAGNOLIA_ACTION_RETRIEVE`, `MAGNOLIA_DOORBELL`, `MAGNOLIA_JOB_NOTIFY`, `MAGNOLIA_CRITIC`.
- The critic keeps the variables of commit acf96ee: `MAGNOLIA_CRITIC_{PROVIDER,MODEL,API_KEY,BASE_URL}`, same seven providers and defaults.
- Probe spending: at most ten short headless calls per client on the member's subscription. Installing Vibe needs LFX's explicit go at execution time.
- Tests run from `opencode_cc_mem/mcp-servers/compchem-memory` with the package's venv: `python -m pytest -q tests/hook`. The full suite (`python -m pytest -q tests`, 877 passed, 4 skipped at acf96ee) must stay green.

## Review Focus

1. **Malformed or partial payloads** (missing `session_id`, tool output as a list of content blocks instead of a string, unknown event name): the CLI must print nothing and exit 0. Test: Task 9 `test_cli_garbage_payloads_exit_zero`.
2. **Two hooks for the same session at once** (Claude and Codex run `async` hooks in parallel): state and transcript writes must not lose rows. Test: Task 3 `test_state_concurrent_updates_keep_all_counts`, Task 7 `test_transcript_concurrent_appends`.
3. **No resolvable project** (cwd outside any workspace, no `.active-project`): the TypeScript action plugin falls back to `projects/xiulian`; the core must write nothing instead of polluting another project. Test: Task 2 `test_resolve_project_none_outside_workspace`, Task 6 `test_action_retrieval_without_project_is_silent`.
4. **Very large or binary tool outputs** (multi-megabyte logs, NUL bytes): the transcript and critic trace must truncate and stay valid JSON lines. Test: Task 7 `test_transcript_truncates_and_escapes`.
5. **Slow memory search** (large store, cold Python start): action retrieval must give up after 2.5 s and log `skipped: "timeout"` rather than stall the tool result. Test: Task 6 `test_action_retrieval_timeout_skips`.

## Alignment with Collegium

Collegium (`HolobiomicsLab/collegium`, `main` at e64e411 on 15 Nov 2026) pins this Magnolia line (c285620, an ancestor of this branch) and reads parts of it through its adapter contract, `src/collegium/harness/magnolia/contract.py`. It also has its own client-neutral ports (`src/collegium/harness/ports.py`: Session, Capture, Inform, Perimeter, Memory, Exposure, Sources, Scrub). D-032 keeps the two repositories independent: Magnolia does not import Collegium and Collegium does not edit Magnolia. This plan therefore duplicates none of Collegium's ports and changes nothing Collegium reads.

**Names this plan must not change** (Collegium's contract at e64e411): `.magnolia/{sessions,runs,staging,entries}/`; `surfacing.jsonl` and its row shape (`ts` str, `channel` in `get_context|boot_context|action_memory`, `store` str, `session_id` str or null, `tier` in `project|staging`, `id` str, `counts_for_exposure` bool, `task_len` int); `action-retrieval.jsonl`; `opencode-transcripts/`; `compchem_memory.opencode_ingest._SECRET_PATTERNS`; `compchem_memory.receipts`; the `MAGNOLIA_*` switches listed there (`MAGNOLIA_DISABLE_INLINE_EXTRACT`, `MAGNOLIA_DISABLE_BACKGROUND_POLLER`, `MAGNOLIA_MEMORY_{ENABLED,MODEL,PROVIDER}`, `MAGNOLIA_PROJECT_DIR`, …). New files sit beside them (`transcripts/<harness>/`, `agent-sessions.jsonl`, `hook-state/`, `hook-errors.jsonl`).

**What each side owns:**
- Magnolia's transcript (Task 7) keeps content for Magnolia's memory, scrubbed with the same `_SECRET_PATTERNS`. Collegium's Capture keeps metadata, byte counts and digests only (Collegium D-065). Two hooks on the same event, written by two owners, are expected on Claude Code and Codex as they are on OpenCode (Collegium D-062); neither calls the other.
- The P0 report (Task 0) is written so that Collegium's second-adapter work (INT-COLLEGIUM-030) can cite it instead of probing again: each finding names its evidence (client version, executable path and sha256, the fixture file holding the verbatim payload), in the spirit of Collegium's `tests/fixtures/collegium_capture_probe/hook_evidence.json`. It also records what Collegium's Perimeter port needs: which built-in tools each client can deny, and how reliably (Collegium D-053).
- Plan 3 (executors, usage ledger) will report a run as Collegium's `SessionOutcome` does: `sessions`, `tool_calls`, `tokens` (`input`, `output`, `reasoning`, `cache_read`, `cache_write`), `cost`, `stopped_because`, `reported_errors`, with `None` for anything the client did not report, never 0; and its process descriptions carry no credential (Collegium's `ProcessSpec`). Recorded here so Plan 3 does not invent a second shape.

**Known gaps left to later plans, named so nobody assumes otherwise:** five readers still know only OpenCode files: `surfacing.py:53`, `server.py:952`, `handover.py:932`, `startup_scan.py:40` (`opencode-sessions.jsonl`) and `dreaming.py:235` (`opencode-transcripts/`). Sessions of Claude Code, Codex or Vibe are invisible to them until Plan 2's `TranscriptSource`.

**For LFX and Tao, not decided here:** whether Collegium's second adapter (INT-COLLEGIUM-030; candidates there: OpenCode without Magnolia, Claude Code, Codex) becomes "Magnolia on another client", a client setting of the existing `magnolia` adapter, once Plan 2 lands. And, on Collegium's side: its `MODEL_CREDENTIAL_NAMES` does not list `MISTRAL_API_KEY`, `ZHIPU_API_KEY`, `ZAI_API_KEY` or `MAGNOLIA_CRITIC_API_KEY`, which commit acf96ee introduces; Collegium's lab would not strip them.

---

### Task 0 (P0): Probe the three clients and record fixtures

**Files:**
- Create: `opencode_cc_mem/scripts/probes/probe_mcp.py` (stdio MCP server with one tool `echo(text) -> text`, using `fastmcp`)
- Create: `opencode_cc_mem/scripts/probes/log_hook.py` (appends `{argv, stdin}` as one JSON line to `$PROBE_LOG`; prints nothing; for the context-injection probe prints the Claude/Codex `additionalContext` JSON read from `$PROBE_INJECT`)
- Create: `opencode_cc_mem/scripts/probes/run_probes.sh` (one function per client; writes into a temporary directory under `$TMPDIR`)
- Create: `opencode_cc_mem/docs/harness-probes-2026-11.md`
- Create: `opencode_cc_mem/mcp-servers/compchem-memory/tests/fixtures/hooks/{claude,codex,vibe}/<event>.json` (one scrubbed payload per event)

**Interfaces:**
- Produces: the fixture files above, read by Plan 2+ adapter tests; the report's answers to the questions below.

- [ ] **Step 1: Claude Code probe.** Settings file with a hook on SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop, SessionEnd, all running `log_hook.py`; `--mcp-config` declaring `probe_mcp.py` as server `probe`; run `claude -p "Call the echo tool with text 'top'. Then ask a subagent to call echo with text 'sub'." --settings <file> --mcp-config <file> --strict-mcp-config --output-format stream-json --verbose --allowedTools mcp__probe__echo Task`. Expected: one log line per event; record the tool name form, whether tool events inside the subagent carry `agent_id`, the Stop payload fields, and the stream-json `result` line.
- [ ] **Step 2: Claude context-size probe.** SessionStart hook returns `additionalContext` of 200 000 characters ending in the marker `MAGNOLIA-END-7Q`; prompt "Reply with the last token of your session context." Record whether the marker comes back (sets the boot-context transport for Plan 2: hook or `--append-system-prompt-file`).
- [ ] **Step 3: Codex probe.** Temporary project with `.codex/config.toml` (`[mcp_servers.probe]`) and `.codex/hooks.json` for the same events; try `codex exec --json -s read-only` first with only the user layer, then ask LFX to trust the project and review the hooks once if project hooks do not load. Record tool name form, `agent_id` on tool events in a subagent, whether a PreToolUse `permissionDecision: "deny"` stops the built-in shell tool (decides Codex as campaign worker, design 8.1), `turn.failed`/`error` shapes.
- [ ] **Step 4: Vibe probe (only after LFX's go to install `mistral-vibe` from PyPI).** `config.toml` with the `probe` server and `hooks.toml` with before-tool, after-tool and after-turn hooks; `vibe --prompt ... --output json`. Record payloads, what an after-tool hook's output does, the instructions file Vibe loads, the skills location, and how to disable its shell tool. If LFX declines, write "not probed" in the report.
- [ ] **Step 5: Scrub and save fixtures.** Replace home paths with `/HOME`, session ids with `ses-fixture-N`, drop any field holding a token or account e-mail. Run: `grep -rniE 'token|bearer|@|/Users/' tests/fixtures/hooks` → no output.
- [ ] **Step 6: Write the report** `harness-probes-2026-11.md`: commands, one table per client (event → fields seen), answers to each question above, and the consequences for sections 5 and 7 of the design. Per client: version, executable path and its `shasum -a 256`; each finding cites the fixture file that shows it (see "Alignment with Collegium"); a table "built-in tools that can be denied, by which mechanism, and whether the probe saw the denial hold".
- [ ] **Step 7: Commit**

```bash
git add opencode_cc_mem/scripts/probes opencode_cc_mem/docs/harness-probes-2026-11.md opencode_cc_mem/mcp-servers/compchem-memory/tests/fixtures/hooks
git commit -m "docs(probes): record Claude Code, Codex and Vibe hook payloads"
```

### Task 1: Event model and tool-name canonicalisation

**Files:**
- Create: `mcp-servers/compchem-memory/src/compchem_memory/hook/__init__.py`, `hook/events.py`
- Test: `mcp-servers/compchem-memory/tests/hook/test_events.py`

**Interfaces:**
- Produces:
  - `SCHEMA_VERSION = 1`
  - `@dataclass ToolCall(name: str, server: str | None, raw_name: str, call_id: str | None, input: dict, output: str | None, is_error: bool = False)`
  - `@dataclass AgentRef(id: str, type: str | None)`
  - `@dataclass HookEvent(harness: str, event: str, session_id: str, cwd: str, prompt: str | None = None, tool: ToolCall | None = None, agent: AgentRef | None = None, assistant_text: str | None = None, transcript_ref: str | None = None)` with `EVENTS = ("session-start", "prompt", "pre-tool", "post-tool", "turn-end", "session-end")` and `HookEvent.from_json(d: dict) -> HookEvent` (raises `ValueError` on unknown event or missing `session_id`)
  - `@dataclass HookResponse(context: str | None = None, notice: str | None = None, tool_note: str | None = None, block: str | None = None)` with `merge(other) -> HookResponse` (texts joined by a blank line)
  - `canonical_tool(raw: str) -> tuple[str | None, str]`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("raw,expected", [
    ("compchem-tools_run_shell", ("compchem-tools", "run_shell")),
    ("mcp__compchem-tools__run_shell", ("compchem-tools", "run_shell")),
    ("compchem-memory_memory_search", ("compchem-memory", "memory_search")),
    ("write", (None, "write")),
    ("Edit", (None, "edit")),
])
def test_canonical_tool(raw, expected):
    assert canonical_tool(raw) == expected

def test_from_json_rejects_unknown_event():
    with pytest.raises(ValueError):
        HookEvent.from_json({"harness": "json", "event": "boot", "session_id": "s", "cwd": "/x"})

def test_response_merge_joins_texts():
    r = HookResponse(context="a").merge(HookResponse(context="b", notice="n"))
    assert r.context == "a\n\nb" and r.notice == "n"
```

- [ ] **Step 2: Run** `python -m pytest -q tests/hook/test_events.py` → FAIL (module missing).
- [ ] **Step 3: Implement** `hook/events.py`. Known servers for the `<server>_<tool>` form: `compchem-tools`, `compchem-memory`, `perspicacite`; built-in names are lower-cased with server `None`. Codex and Vibe forms from the Task 0 report are added to the parametrised test and the function.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(hook): event model and canonical tool names`.

### Task 2: Project resolution

**Files:**
- Create: `hook/project.py`
- Test: `tests/hook/test_project.py`

**Interfaces:**
- Produces: `@dataclass ProjectPaths(root: Path, project_dir: Path, store: Path, name: str, root_mode: bool)`; `resolve_project(cwd: Path, env: Mapping[str, str]) -> ProjectPaths | None`.

Rules, in order (copied from the plugins' `isProjectRoot`/`workspaceRoot`/`.active-project` logic): project-root mode when `MAGNOLIA_PROJECT_WORKSPACE=1` or `cwd/.magnolia/GOAL.md` or `cwd/.magnolia/boot-context.md` exists (root = first ancestor holding `mcp-servers/compchem-memory` or `rules`); else `cwd/.magnolia/.active-project` names `projects/<name>`; else absolute `MAGNOLIA_PROJECT_DIR`; else `None`. No `projects/xiulian` fallback.

- [ ] **Step 1: Write the failing tests** `test_resolve_project_root_mode_by_goal`, `test_resolve_project_active_project_marker`, `test_resolve_project_env_absolute`, `test_resolve_project_none_outside_workspace` (tmp_path layouts; assert `store == project_dir / ".magnolia"` and `None` for the last).
- [ ] **Step 2: Run** `python -m pytest -q tests/hook/test_project.py` → FAIL.
- [ ] **Step 3: Implement** `resolve_project`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): project resolution without the xiulian fallback`.

### Task 3: Per-session state

**Files:**
- Create: `hook/state.py`
- Test: `tests/hook/test_state.py`

**Interfaces:**
- Produces: `class SessionState` used as a context manager: `with SessionState.open(store: Path, harness: str, session_id: str) as st:` exposes `st.data: dict` and saves on exit under an exclusive `fcntl.flock` on `<store>/hook-state/<harness>-<session_id>.json.lock`. Keys used by later tasks: `directive_sent: bool`, `injections: int`, `entry_counts: dict[str, int]`, `skips_logged: list[str]`, `query_cache: dict[str, {"ts": float, "hits": list}]`, `pending_application: list[dict]`, `unshown_notices: list[str]`. Session ids are sanitised to `[A-Za-z0-9_.-]`.

- [ ] **Step 1: Write the failing tests** `test_state_roundtrip`, `test_state_sanitises_session_id`, and

```python
def test_state_concurrent_updates_keep_all_counts(tmp_path):
    def bump():
        with SessionState.open(tmp_path, "claude", "s1") as st:
            st.data["injections"] = st.data.get("injections", 0) + 1
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda _: bump(), range(40)))
    with SessionState.open(tmp_path, "claude", "s1") as st:
        assert st.data["injections"] == 40
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with an atomic write (`atomic_io.py` if its helper fits, else temp file + `os.replace`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): locked per-session state file`.

### Task 4: Boot context and retrieval directive

**Files:**
- Create: `hook/boot_context.py`, `hook/directive.py`
- Test: `tests/hook/test_boot_context.py`, `tests/hook/test_directive.py`, `tests/hook/test_parity_plugins.py` (Node driver, skipped without `node`, as in `tests/test_claim_critic_plugin.py`)

**Interfaces:**
- Consumes: `ProjectPaths`, `SessionState`.
- Produces: `MAX_PAYLOAD_CHARS = 256_000`; `build_boot_payload(paths: ProjectPaths) -> str`; `DIRECTIVE: str` (verbatim from the plugin, leading `"\n\n"` included); `retrieval_directive(paths, state, session_id) -> str | None` (returns the directive once per session, appends `{ts, sessionID, action: "inject"}` to `<store>/auto-retrieval.jsonl`; `None` when `MAGNOLIA_AUTORETRIEVE` is `0/off/false/no`).

- [ ] **Step 1: Write the failing tests**: header lines `<!-- magnolia-boot-context:<name> -->` and `[MAGNOLIA BOOT CONTEXT — project <name>]`; sections `--- boot-context.md ---`, `--- audit-report.md ---`, `--- rules/<file> ---` in sorted order; truncation suffix `"\n\n[truncated at 256k chars]"`; directive once then `None`; kill switch. Parity: `test_boot_payload_matches_plugin` builds a fixture workspace, runs `magnolia-boot-context.ts` `setup()` under Node with a fake `ctx.session.hook("context")` that captures `event.system`, and asserts equality with `build_boot_payload`.
- [ ] **Step 2: Run** `python -m pytest -q tests/hook/test_boot_context.py tests/hook/test_directive.py tests/hook/test_parity_plugins.py` → FAIL.
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): boot context and session-start directive`.

### Task 5: Doorbell

**Files:**
- Create: `hook/doorbell.py`
- Test: `tests/hook/test_doorbell.py`; add a parity case to `tests/hook/test_parity_plugins.py`

**Interfaces:**
- Produces: `DEFAULT_AGENTS = ("literature", "xiulian")`; `registry_agents(root: Path) -> list[str]` (defaults plus `projects/*/agent.json` not `enabled: false`); `@dataclass DoorbellResult(text: str, sent: list[str], letters: list[Path])`; `process_prompt(text: str, paths: ProjectPaths) -> DoorbellResult`. Replacement text `[→ sent to @<agent>; the agents daemon will run it]`; letter format and path `projects/<agent>/inbox/from-<source>/<YYYY-MM-DD>_<slug>_<hhmmss>.task.md` as in the plugin; bare `@agent` with no request left untouched; `MAGNOLIA_DOORBELL=0` disables.

- [ ] **Step 1: Write the failing tests** `test_doorbell_writes_letter_and_rewrites`, `test_doorbell_bare_mention_untouched`, `test_doorbell_disabled_agent_ignored`, `test_doorbell_kill_switch`; parity `test_doorbell_matches_plugin` (same input text through the v2 prompt hook under Node; compare rewritten text and letter body, ignoring the timestamp line).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): doorbell letters`.

### Task 6: Action-time retrieval

**Files:**
- Modify: `mcp-servers/compchem-memory/src/compchem_memory/quick_search.py:36-80` — extract `search(query: str, project_dir: str, k: int = 5) -> list[dict]` (the dicts `main()` prints today); `main()` calls it.
- Create: `hook/action_retrieval.py`
- Test: `tests/hook/test_action_retrieval.py`; existing quick_search tests stay green.

**Interfaces:**
- Consumes: `ToolCall`, `ProjectPaths`, `SessionState`, `quick_search.search`, `surfacing.log_surfacing`.
- Produces:
  - `ACTION_TOOLS: frozenset[str]` in canonical form: `compchem-tools.run_shell`, `compchem-tools.submit_job`, `submit_job`, `write`, `edit`, `compchem-tools.gnina_dock`, `compchem-tools.gromacs_run`, `compchem-tools.gromacs_setup`, `compchem-tools.haddock3_run`, `compchem-tools.gaussian_run`, `compchem-tools.orca_run`, `compchem-tools.xtb_optimize`, `compchem-tools.xtb_singlepoint`, `compchem-tools.preprocess_pdb`, `compchem-tools.run_acpype` (canonical key = `f"{server}.{name}"` or bare `name`).
  - `args_to_query(args: object) -> str` (string leaves, depth ≤ 2, each < 2000 chars, joined, cut at 400).
  - `on_post_tool(event: HookEvent, paths: ProjectPaths, state: SessionState) -> str | None` returning the block text that the plugin prepends today (`[Magnolia · action-memory] N past learning(s) matched this action (tool: <short>). ...`).
  - `probe_application(paths, state, session_id: str, reply_text: str) -> None` (the plugin's lexical probe; rows `event: "application"`).

The search runs at post-tool time (one process per event cannot keep the plugin's pre-tool promise). It runs in a worker thread joined with a 2.5 s timeout; on timeout log `{skipped: "timeout"}` and return `None`. The query cache moves to `state.data["query_cache"]` with the 120 s TTL. Surfacing rows go through `surfacing.log_surfacing(store, sources, channel="action_memory", session_id=...)`, replacing the plugin's hand-written rows.

- [ ] **Step 1: Write the failing tests**: `test_args_to_query_depth_and_cut`; `test_action_retrieval_injects_block` (fixture store with three entries, two above score 3; assert header line, bullet format `- [project/<type>] <title> — <gist>\n  <path>`, ledger rows); `test_action_retrieval_latch_and_budget` (third time an entry is offered it is dropped; ninth injection logs `skipped: "budget"` once); `test_action_retrieval_ignores_non_action_tools`; `test_action_retrieval_without_project_is_silent`; `test_action_retrieval_timeout_skips` (monkeypatch `quick_search.search` to sleep 3 s; assert `None` and a `timeout` row); `test_probe_application_marks_applied`; `test_action_surfacing_rows_keep_collegium_shape` (every row the task writes passes a copy of the predicate in "Alignment with Collegium": field names, types and the allowed `channel`/`tier` values; the copy names its source, `collegium/src/collegium/harness/magnolia/exposure.py` at e64e411).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `quick_search.search`, then `action_retrieval.py`.
- [ ] **Step 4: Run** `python -m pytest -q tests/hook/test_action_retrieval.py tests -k quick_search` → PASS.
- [ ] **Step 5: Commit** `feat(hook): action-time retrieval in Python`.

### Task 7: Magnolia transcript and session mapping

**Files:**
- Create: `hook/transcript.py`, `hook/sessions.py`
- Test: `tests/hook/test_transcript.py`, `tests/hook/test_sessions.py`

**Interfaces:**
- Produces:
  - `transcript_path(store: Path, harness: str, session_id: str) -> Path` = `<store>/transcripts/<harness>/<session_id>.jsonl`
  - `append_event(store: Path, event: HookEvent) -> None`: rows `{schema_version, ts, kind, ...}` with `kind` in `prompt` (`text`), `tool` (`name`, `call_id`, `input`, `output`, `is_error`, `agent`), `assistant` (`text`), `session-start`, `session-end`; outputs cut at 2000 characters, inputs at 4000 characters of their JSON, text cleaned of NUL bytes, then every string field passed through `opencode_ingest._SECRET_PATTERNS` (the scrubber the OpenCode ingest path already applies); one `os.write` per row under `fcntl.flock`.
  - `read_turns(path: Path) -> list[dict]` (rows in order, malformed lines skipped).
  - `record_session(store: Path, harness: str, session_id: str, cwd: str, transcript_ref: str | None) -> bool` writes `{ts, harness, session_id, directory, transcript_ref}` once per session to `<store>/agent-sessions.jsonl`; returns whether it wrote.
  - `iter_sessions(store: Path) -> Iterator[dict]` yields rows of `agent-sessions.jsonl` plus legacy `opencode-sessions.jsonl` rows mapped to `{harness: "opencode", session_id: <opencode_session_id>, ...}`.

- [ ] **Step 1: Write the failing tests**: `test_transcript_rows_by_kind`, `test_transcript_truncates_and_escapes` (5 MB output with `\x00`; row parses, output length 2000), `test_transcript_concurrent_appends` (8 threads × 50 rows → 400 parseable rows), `test_transcript_scrubs_secrets` (an `OPENAI_API_KEY=sk-…` in a tool input and a bearer token in an output are replaced as `opencode_ingest` replaces them), `test_record_session_once`, `test_iter_sessions_merges_legacy`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): Magnolia transcript and agent-sessions mapping`.

### Task 8: Critic in Python on the shared LLM client

**Files:**
- Modify: `mcp-servers/compchem-memory/src/compchem_memory/llm.py` — add `@dataclass Route(provider, model, url, key, schema)`, `resolve_route(role: Literal["memory", "critic"]) -> Route | None` (role `memory` reproduces `_resolve_call` + `_get_api_key` + `_get_base_url`; role `critic` reads `MAGNOLIA_CRITIC_{PROVIDER,MODEL,API_KEY,BASE_URL}` with the same seven providers, defaults and order as `claim-critic.ts`), and `call_route(route, system: str, user: str, *, max_tokens: int, temperature: float | None = None, json_mode: bool = False, disable_thinking: bool = False) -> str | None` (never raises). `call_llm` keeps its signature and behaviour.
- Create: `hook/critic.py`, `hook/notices.py`
- Test: `tests/hook/test_critic.py`, `tests/hook/test_notices.py`; existing `tests/test_llm_providers.py` stays green.

**Interfaces:**
- Consumes: `read_turns`, `Route`, `call_route`, `drain_job_notices`.
- Produces:
  - `build_trace(rows: list[dict]) -> tuple[str, str]` → `(report, trace_text)`: report = assistant text after the last prompt; trace = every tool row (current turn 2000-character outputs, earlier turns 500, `memory_get_context`, `memory_get_run_history`, `memory_search`, `memory_scan_headers` uncapped), JSON, cut at 80 000 with `"\n…(truncated)"`.
  - `run_critic(paths: ProjectPaths, harness: str, session_id: str, state: SessionState) -> None`: no-op unless `MAGNOLIA_CRITIC` is set and report ≥ 200 characters; writes the same verdict row as the plugin to `<store>/claim-critic/<session_id>.jsonl` (plus `harness`), errors to `errors.jsonl`; a flagged verdict appends `⚠ (<severity>) <why[:140]>` to `state.data["unshown_notices"]`.
  - `pending_notices(paths, state) -> str | None`: drains job notices (format `<icon> <tool> finished (<state>) — run …<last 24 of run_id>`, cut at 160) and `unshown_notices`, one per line.

- [ ] **Step 1: Write the failing tests**: `test_resolve_route_critic_matches_plugin` (parametrised over the eight cases of `tests/test_claim_critic_plugin.py`, comparing `Route.url/model/key`); `test_call_route_anthropic_and_openai_shapes` (monkeypatched `httpx.post`); `test_build_trace_caps`; `test_run_critic_writes_verdict_and_notice`; `test_run_critic_logs_http_error`; `test_pending_notices_drains_jobs_and_flags`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `Route`, `resolve_route`, `call_route`, then `critic.py`, `notices.py`.
- [ ] **Step 4: Run** `python -m pytest -q tests/hook/test_critic.py tests/hook/test_notices.py tests/test_llm_providers.py` → PASS.
- [ ] **Step 5: Commit** `feat(hook): claim critic and notices on the shared LLM client`.

### Task 9: Dispatcher and CLI

**Files:**
- Create: `hook/dispatch.py`, `hook/__main__.py`, `hook/adapters/__init__.py`, `hook/adapters/json_adapter.py`
- Test: `tests/hook/test_dispatch.py`, `tests/hook/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `handle(event: HookEvent, env: Mapping[str, str]) -> HookResponse`, routing: `session-start` → `record_session`, transcript, boot payload + directive as `context`; `prompt` → transcript, doorbell (`context` = "The following @mentions were already sent to helper agents; do not act on them: …" when letters were written), directive if not yet sent, `pending_notices` as `notice`; `pre-tool` → transcript only; `post-tool` → transcript, action retrieval as `tool_note`, `pending_notices`; `turn-end` → transcript (`assistant_text`), `probe_application`, `run_critic`; `session-end` → transcript.
  - Adapter protocol in `adapters/__init__.py`: `parse(raw: dict, event: str) -> HookEvent`, `render(resp: HookResponse, event: HookEvent) -> str` (stdout text); `ADAPTERS = {"json": json_adapter}` (Plan 2 adds `claude`, `codex`, `vibe`).
  - CLI: `python -m compchem_memory.hook <harness> <event>` reads stdin, prints `render(...)`, always exits 0 except when `resp.block` is set (exit 2, reason on stderr); any exception is written to `<store>/hook-errors.jsonl` (or `~/.local/state/magnolia/hook-errors.jsonl` when no project resolves) and the CLI prints nothing.

- [ ] **Step 1: Write the failing tests**: `test_dispatch_session_start_context`, `test_dispatch_prompt_doorbell_context`, `test_dispatch_post_tool_note`, `test_dispatch_turn_end_runs_critic` (critic monkeypatched), and

```python
@pytest.mark.parametrize("stdin", ["", "not json", "{}", '{"event":"prompt"}',
                                   '{"harness":"json","event":"post-tool","session_id":"s","cwd":"/nope","tool":{"raw_name":"x","output":[1,2]}}'])
def test_cli_garbage_payloads_exit_zero(stdin):
    proc = subprocess.run([sys.executable, "-m", "compchem_memory.hook", "json", "post-tool"],
                          input=stdin, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and proc.stdout == ""
```

  plus `test_cli_cold_start_under_budget` (median of 5 `session-start` runs on a fixture workspace < 1.0 s; record the measured value in the commit message).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; keep imports in `__main__` lazy (import behaviour modules inside `handle` branches).
- [ ] **Step 4: Run** `python -m pytest -q tests/hook` → PASS, then the full suite `python -m pytest -q tests` → 877+ passed, 4 skipped, and `python -m pytest -q ../../tests/test_claim_critic_plugin.py ../../tests/test_campaign_reviewers.py` → PASS.
- [ ] **Step 5: Commit** `feat(hook): dispatcher and CLI entry point`.

### Task 10: Hand-back for review

**Files:**
- Modify: `opencode_cc_mem/docs/harness-neutral-agents-design.md` — section 9: P0 and P1 marked done with commit ids; section 5 updated from the probe report.
- Modify: `docs/harness-adaptation.md` — one paragraph pointing to the hook core and stating that OpenCode still uses the TypeScript plugins.

- [ ] **Step 1: Update both documents.**
- [ ] **Step 2: Run** `git diff --check` → no output; full suite as in Task 9 → PASS.
- [ ] **Step 3: Commit** `docs: record P0 and P1 of the harness-neutral design`. Nothing is pushed; the branch goes to Tao for review (design section 9).
