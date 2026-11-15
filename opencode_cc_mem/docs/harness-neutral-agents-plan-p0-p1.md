# Harness-neutral Magnolia — Plan 1: client probes (P0) and the hook core (P1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record how Claude Code, Codex and Vibe call hooks around MCP tool calls (P0), then build Magnolia's client-neutral hook core in Python with the behaviours of today's OpenCode plugins (P1), without changing the OpenCode path.

**Architecture:** A package `compchem_memory.hook` receives one normalised `HookEvent`, runs the behaviour for that event (boot context, retrieval directive, doorbell, action retrieval, transcript, session mapping, notices, critic) and returns one `HookResponse`. Each process handles one event; state that the TypeScript plugins kept in memory lives in a per-session JSON file. Client adapters (Claude Code, Codex, Vibe) are Plan 2 onwards; P1 ships only a `json` adapter used by tests.

**Tech Stack:** Python 3.11+ (compchem-memory package), pytest, Node 24 for the parity drivers, Claude Code 2.1.325 and codex-cli 0.154.0 for the probes.

**Spec:** `opencode_cc_mem/docs/harness-neutral-agents-design.md` (approved by LFX, 15 Nov 2026). Sections 5, 6 and 9 (P0, P1) are implemented here; sections 7 and 8 belong to later plans, written after the P0 report.

**Audit:** read by an independent Codex worker (Astra, max, two Sol leaves) on 15 Nov 2026 against the design, the plugins and Collegium `main` e64e411; verdict "build after these fixes"; its fifteen findings are folded in below (F1–F15 where cited). Run record: `~/.codex-runs/magnolia/plan1-audit-001/result.json`.

**Deferred to later plans (written after the P0 report):** client adapters for Claude Code, Codex and Vibe, the `--harness` launcher, rendered client configurations, skills linking and client-neutral rules prose (Plan 2, design P2–P3, P5); distillation and handover reading the Magnolia transcript through a `TranscriptSource` (Plan 2, with the first non-OpenCode sessions); routing, executors and the usage governor (Plan 3, design P4); OpenCode plugins as shims (P6, Tao's call).

## Global Constraints

- Nothing in `.opencode/plugins/` or `softwares/bin/magnolia` changes in this plan (design G5, phase table: "OpenCode plugins untouched").
- Magnolia never reads, copies or sends any client's subscription credentials (design A1). Probes use the clients' own logins as they are; no `--bare`.
- A hook failure never breaks the session: the CLI always exits 0 and prints nothing on error, except a deliberate `block` (design 6.1); errors go to `<store>/hook-errors.jsonl`.
- `HookEvent` and transcript rows carry `schema_version: 1` (design section 11).
- Exact texts and limits copied from the plugins: directive text (`magnolia-auto-retrieval.ts:40-49`), boot payload header and a 256 000-character prefix followed by the existing truncation suffix (`magnolia-boot-context.ts:24, 116-143`), doorbell letter and replacement text (`magnolia-agent-doorbell.ts:93-143`), action block text, `MIN_SCORE = 3`, `MAX_HITS = 4`, `MAX_INJECTIONS_PER_SESSION = 8`, `MAX_PER_ENTRY_PER_SESSION = 2`, `QUERY_TIMEOUT_MS = 2500`, `CACHE_TTL_MS = 120000` (`magnolia-action-retrieval.ts:64-69, 331-363`), critic caps 2000 / 500 / 80 000 / min report 200 characters, evidence tools uncapped (`claim-critic.ts:55-82`), job notices 10 per batch (`magnolia-job-notify.ts:33-35`). Where P1 deliberately departs from a plugin, the task says so in a line beginning "Deliberate change:".
- State locks are held only to read and to commit state; never across a memory search, a file scan of the store or an LLM call (audit F7).
- Everything P1 builds works with no model credential in the environment: boot, directive, doorbell, retrieval and transcript run; the critic is then a silent no-op. Test: Task 9 `test_core_without_any_model_credential`.
- Kill switches keep their names: `MAGNOLIA_AUTORETRIEVE`, `MAGNOLIA_ACTION_RETRIEVE`, `MAGNOLIA_DOORBELL`, `MAGNOLIA_JOB_NOTIFY`, `MAGNOLIA_CRITIC`.
- The critic keeps the variables of commit acf96ee: `MAGNOLIA_CRITIC_{PROVIDER,MODEL,API_KEY,BASE_URL}`, same seven providers and defaults.
- Probe spending: at most ten short headless calls per client on the member's subscription. Installing Vibe needs LFX's explicit go at execution time.
- Tests run from `opencode_cc_mem/mcp-servers/compchem-memory` with the package's venv: `python -m pytest -q tests/hook`. The full suite (`python -m pytest -q tests`) must stay green; 877 passed, 4 skipped at acf96ee is the historical baseline, and each task records the counts it actually observed. Parity tests run under Node in the acceptance environment: a parity run where every case skipped does not count as passing.

## Review Focus

1. **Malformed or partial payloads** (missing `session_id`, tool output as a list of content blocks instead of a string, unknown event name): the CLI must print nothing and exit 0. Test: Task 9 `test_cli_garbage_payloads_exit_zero`.
2. **Two hooks for the same session at once** (Claude and Codex run `async` hooks in parallel): state and transcript writes must not lose rows. Test: Task 3 `test_state_concurrent_updates_keep_all_counts`, Task 7 `test_transcript_concurrent_appends`.
3. **No resolvable project** (cwd outside any workspace, no `.active-project`): the TypeScript action plugin falls back to `projects/xiulian`; the core must write nothing instead of polluting another project. Test: Task 2 `test_resolve_project_none_outside_workspace`, Task 6 `test_action_retrieval_without_project_is_silent`.
4. **Very large or binary tool outputs** (multi-megabyte logs, NUL bytes): the transcript and critic trace must truncate and stay valid JSON lines. Test: Task 7 `test_transcript_truncates_and_escapes`.
5. **Slow memory search** (large store, cold Python start): the whole hook process, imports and exit included, must finish within its deadline and log `skipped: "timeout"` rather than stall the tool result; a thread joined with a timeout does not achieve this on its own. Test: Task 6 `test_action_retrieval_timeout_skips` and Task 9 `test_cli_post_tool_exits_within_deadline`.
6. **A session whose project marker changes mid-session, or whose client sends no session-start** (Vibe): the session stays bound to the project of its first event. Test: Task 2 `test_binding_survives_marker_change`, `test_binding_without_session_start`.
7. **A secret in a tool call** whose value has no recognisable prefix (a `"password": "…"` field): it never reaches the transcript or the critic. Test: Task 7 `test_transcript_scrubs_secrets`.

## Alignment with Collegium

Collegium (`HolobiomicsLab/collegium`, `main` at e64e411 on 15 Nov 2026) pins this Magnolia line (c285620, an ancestor of this branch) and reads parts of it through its adapter contract, `src/collegium/harness/magnolia/contract.py`. It also has its own client-neutral ports (`src/collegium/harness/ports.py`: Session, Capture, Inform, Perimeter, Memory, Exposure, Sources, Scrub). D-032 keeps the two repositories independent: Magnolia does not import Collegium and Collegium does not edit Magnolia. This plan duplicates none of Collegium's ports and must change nothing Collegium reads or calls. That includes the private helpers Collegium calls directly: `llm._resolve_call()` (`collegium@e64e411:src/collegium/worker.py:244-250`), and with it `_get_api_key(provider)` and `_get_base_url(provider)`, which Magnolia's own provider tests also call (`tests/test_llm_providers.py:106-148, 274-284`); Task 8 keeps all three with their current signatures and results.

**Names this plan must not change** (Collegium's contract at e64e411): `.magnolia/{sessions,runs,staging,entries}/`; `surfacing.jsonl` and its row shape (`ts` str, `channel` in `get_context|boot_context|action_memory`, `store` str, `session_id` str or null, `tier` in `project|staging`, `id` str, `counts_for_exposure` bool, `task_len` int); `action-retrieval.jsonl`; `opencode-transcripts/`; `compchem_memory.opencode_ingest._SECRET_PATTERNS`; `compchem_memory.receipts`; the `MAGNOLIA_*` switches listed there (`MAGNOLIA_DISABLE_INLINE_EXTRACT`, `MAGNOLIA_DISABLE_BACKGROUND_POLLER`, `MAGNOLIA_MEMORY_{ENABLED,MODEL,PROVIDER}`, `MAGNOLIA_PROJECT_DIR`, …). New files sit beside them (`transcripts/<harness>/`, `agent-sessions.jsonl`, `hook-state/`, `hook-errors.jsonl`).

**What each side owns:**
- Magnolia's transcript (Task 7) keeps content for Magnolia's memory, scrubbed by `opencode_ingest.scrub_secrets` (key-aware JSON-field redaction, then `_SECRET_PATTERNS`). Collegium's Capture keeps metadata, byte counts and digests only (Collegium D-065). Magnolia and Collegium may each install their own hook on the same event; D-062 establishes this only for OpenCode, so coexistence on Claude Code and Codex is a P0 observation (Task 0) and an adapter test (Plan 2), not an assumption. Neither calls the other.
- The P0 report (Task 0) is written so that Collegium's second-adapter work (INT-COLLEGIUM-030) can cite it instead of probing again: each finding names its evidence (client version, executable path and sha256, the fixture file holding the verbatim payload), in the spirit of Collegium's `tests/fixtures/collegium_capture_probe/hook_evidence.json`. It records the harness-level denial controls each client offers and their limits; this is not evidence of the operating-system bound Collegium D-053 requires.
- Plan 3 (executors, usage ledger) mirrors Collegium's `SessionOutcome`: `sessions`, `tool_calls`, `tokens`, `cost`, `stopped_because`, `reported_errors`, with the aggregate set to `None` when the client reported nothing (Collegium's `TokenCounts` components are ints). If Plan 3 keeps unknown token components apart, it defines that as a versioned schema and its conversion to `TokenCounts`. Process descriptions carry no credential (Collegium's `ProcessSpec`).
- Reusable by Collegium without either repository importing the other: the versioned fixtures and the normalised event/response contract of Tasks 0, 1 and 9 (tool, call, turn and agent identities; which response fields a client actually emitted). No second Collegium adapter is declared implemented by this plan.

**Known gaps left to later plans, named so nobody assumes otherwise:** these readers do not consume the new `transcripts/<harness>/` stream or `agent-sessions.jsonl`: `surfacing.py:53` (including `guess_session_id`), `server.py:952`, `handover.py:932`, `startup_scan.py:40` (`opencode-sessions.jsonl`, with its existing tool-event fallback, `:31-45, 64-86`) and `dreaming.py:235-244` (`opencode-transcripts/`, then `sessions/<sid>.jsonl`). Plan 2 migrates transcript selection and session attribution, keeps the legacy lookup order, and adds a regression fixture from Collegium's own materialised `opencode-transcripts` JSON (`collegium@e64e411:src/collegium/worker.py:105-129`, `tests/test_worker.py:89-98`). Plan 2's skills linking keeps `.opencode/skills` as a source, which Collegium's contract and bootstrap read (`contract.py:84-89`, `scripts/bootstrap.sh:38-55`).

**For LFX and Tao, not decided here:** whether Collegium's second adapter (INT-COLLEGIUM-030; candidates there: OpenCode without Magnolia, Claude Code, Codex) becomes "Magnolia on another client", a client setting of the existing `magnolia` adapter, once Plan 2 lands. And, on Collegium's side: its `MODEL_CREDENTIAL_NAMES` does not list `MISTRAL_API_KEY`, `ZHIPU_API_KEY`, `ZAI_API_KEY` or `MAGNOLIA_CRITIC_API_KEY`, which commit acf96ee introduces; when Collegium moves its pin past acf96ee, its lab would not strip them (a follow-up on Collegium's side, not a Plan 1 regression).

---

### Task 0 (P0): Probe the three clients and record fixtures

**Files:**
- Create: `opencode_cc_mem/scripts/probes/probe_mcp.py` (stdio MCP server with one tool `echo(text) -> text`, using `fastmcp`)
- Create: `opencode_cc_mem/scripts/probes/log_hook.py` (appends `{argv, stdin}` as one JSON line to `$PROBE_LOG`; prints nothing; for the context-injection probe prints the Claude/Codex `additionalContext` JSON read from `$PROBE_INJECT`)
- Create: `opencode_cc_mem/scripts/probes/run_probes.sh` (one function per client; writes into a temporary directory under `$TMPDIR`)
- Create: `opencode_cc_mem/docs/harness-probes-2026-11.md`
- Create: `opencode_cc_mem/mcp-servers/compchem-memory/tests/fixtures/hooks/<client>/<scenario>/<nn>-<event>.json` (scrubbed payloads in arrival order; scenarios `parent`, `subagent`, `tool-error`, `context-small`, `context-max`, `deny-shell`, `deny-write`, `two-hooks`; each scenario directory holds a `scenario.json` with the command, client version and what was observed)

**Interfaces:**
- Produces: the fixture files above, read by Plan 2+ adapter tests; the report's answers to the questions below.

Every step first asserts that the hooks loaded (a log line from the first event); a run with no hook line is a failed probe, not a negative finding, because Claude Code's print mode can ignore invalid settings silently (`claude --help`). Anything not observed is reported as "unknown", never as absent.

- [ ] **Step 1: Claude Code probe.** Settings file with a hook on SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop, SessionEnd, all running `log_hook.py`, plus a second, independent hook command on PostToolUse (`two-hooks`); `--mcp-config` declaring `probe_mcp.py` as server `probe`; run `claude -p "Call the echo tool with text 'top'. Then ask a subagent to call echo with text 'sub'." --settings <file> --setting-sources user --mcp-config <file> --strict-mcp-config --output-format stream-json --verbose --allowedTools mcp__probe__echo Task`. Record: tool name form; whether tool events inside the subagent carry `agent_id`; order of parent and child events; whether both PostToolUse hooks ran and in what order; behaviour of an `async: true` hook; the Stop payload fields; the stream-json `result` line; an `echo` failure (`tool-error`).
- [ ] **Step 2: Context-size probe, Claude and Codex.** A control of 2 000 characters, then the real maximum boot payload: a 256 000-character prefix plus the truncation suffix, ending in the marker `MAGNOLIA-END-7Q`, returned as SessionStart `additionalContext`; prompt "Reply with the last token of your session context." Record separately: bytes emitted, whether the client accepted the hook output, and whether the model reported the marker. This sets the boot-context transport for Plan 2 (hook, `--append-system-prompt-file`, or a file the agent reads).
- [ ] **Step 3: Codex probe.** `git init` a temporary project (or pass `--skip-git-repo-check`) and run with an explicit `-C <dir>`; `.codex/config.toml` (`[mcp_servers.probe]`) and `.codex/hooks.json` for the same events and a second independent PostToolUse hook; try `codex exec --json` first with only the user layer, then ask LFX to trust the project and review the hooks once if project hooks do not load, and confirm a positive run with the project configuration loaded. Record tool name form, `agent_id` on tool events in a subagent (`subagent`), `turn.failed`/`error` shapes. Denial, in `-s workspace-write` so that a sandbox refusal cannot pass for a hook decision: (a) a PreToolUse `permissionDecision: "deny"` on the built-in shell (`deny-shell`); (b) a write outside the project (`deny-write`); for each, name the mechanism that stopped it from the event log (hook, sandbox, or none). Both decide Codex as campaign worker (design 8.1).
- [ ] **Step 4: Vibe probe (only after LFX's go to install `mistral-vibe` from PyPI).** `config.toml` with the `probe` server and `hooks.toml` with before-tool, after-tool and after-turn hooks; `vibe --prompt ... --output json`. Record payloads, what an after-tool hook's output does, whether a subagent exists and what its events carry, the instructions file Vibe loads, the skills location, and how to disable its shell tool. If LFX declines, write "not probed" in the report.
- [ ] **Step 4b: Usage-limit texts.** From each client's documentation and help, and from any limit message met during the probes, record the text a client prints when a plan limit is reached. Never exhaust a subscription to provoke one.
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
  - `@dataclass ToolCall(name: str, server: str | None, raw_name: str, call_id: str | None, input: dict, output: str | None, is_error: bool = False)`; `output` is always a string or `None` here: adapters normalise a native list or object output to text before building a `ToolCall` (`normalise_output(value: object) -> str | None`, defined here: strings as is, lists of content blocks joined by their `text` fields, other JSON values serialised).
  - `@dataclass AgentRef(id: str, type: str | None)`; `agent=None` means the main agent.
  - `@dataclass HookEvent(harness: str, event: str, session_id: str, cwd: str, schema_version: int = SCHEMA_VERSION, turn_id: str | None = None, prompt: str | None = None, tool: ToolCall | None = None, agent: AgentRef | None = None, assistant_text: str | None = None, transcript_ref: str | None = None)` with `EVENTS = ("session-start", "prompt", "pre-tool", "post-tool", "turn-end", "session-end", "agent-start", "agent-end")` and `HookEvent.from_json(d: dict) -> HookEvent` (raises `ValueError` on an unknown event, a missing `session_id` or an unsupported `schema_version`). `cwd` is the raw location the client reported; the resolved, bound project is `ProjectPaths` (Task 2), never `cwd`.
  - `@dataclass Block(reason: str)`; `@dataclass HookResponse(context: str | None = None, notice: str | None = None, tool_note: str | None = None, block: Block | None = None)` with `merge(other) -> HookResponse` (texts joined by a blank line; the first block wins)
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

def test_from_json_rejects_unsupported_version():
    with pytest.raises(ValueError):
        HookEvent.from_json({"harness": "json", "event": "prompt", "session_id": "s", "cwd": "/x",
                             "schema_version": 2})

@pytest.mark.parametrize("value,expected", [
    ("plain", "plain"),
    ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "a\nb"),
    ({"k": 1}, '{"k": 1}'),
    (None, None),
])
def test_normalise_output(value, expected):
    assert normalise_output(value) == expected
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
- Produces: `@dataclass ProjectPaths(root: Path, project_dir: Path, store: Path, name: str, root_mode: bool)`; `resolve_project(cwd: Path, env: Mapping[str, str]) -> ProjectPaths | None`; `bind_project(harness: str, session_id: str, cwd: Path, env: Mapping[str, str]) -> ProjectPaths | None`; `state_home(env) -> Path` = `$XDG_STATE_HOME/magnolia` or `~/.local/state/magnolia`.

Rules of `resolve_project`, in order: project-root mode when `MAGNOLIA_PROJECT_WORKSPACE=1` or `cwd/.magnolia/GOAL.md` or `cwd/.magnolia/boot-context.md` exists (root = first ancestor holding `mcp-servers/compchem-memory` or `rules`); else `cwd/.magnolia/.active-project` names `projects/<name>`; else `MAGNOLIA_PROJECT_DIR`, resolved against `cwd` when relative, as the plugins do (`magnolia-action-retrieval.ts:172-181`, `magnolia-job-notify.ts:110-117`, `claim-critic.ts:153-160`); else `None`. The plugins do not agree on the root rule; this one rule is a normalisation of them, stated as a "Deliberate change:" in the module docstring. No `projects/xiulian` fallback.

`bind_project` is what every behaviour uses. The plugins bind a session to its project on its first event so that a later change of `.active-project` cannot move it (`magnolia-boot-context.ts:17-19, 151-163`, `magnolia-session-capture.ts:85-95`). The binding is a file `<state_home>/bindings/<harness>-<session_id>.json` holding the resolved `ProjectPaths`, created with `O_CREAT | O_EXCL` on the first event of any kind (a client without session-start, such as Vibe, binds on its first tool or turn event); an existing binding wins over a fresh resolution. A session that resolves to `None` is bound to `None` too, and stays silent.

- [ ] **Step 1: Write the failing tests** `test_resolve_project_root_mode_by_goal`, `test_resolve_project_active_project_marker`, `test_resolve_project_env_absolute`, `test_resolve_project_env_relative_to_cwd`, `test_resolve_project_none_outside_workspace` (tmp_path layouts; assert `store == project_dir / ".magnolia"` and `None` for the last); `test_binding_survives_marker_change` (bind with marker A in one subprocess, switch the marker to B, bind again in another subprocess: still A); `test_binding_concurrent_first_events` (8 processes bind the same session at once: one file, every process gets the same project); `test_binding_without_session_start` (first event `post-tool`: bound).
- [ ] **Step 2: Run** `python -m pytest -q tests/hook/test_project.py` → FAIL.
- [ ] **Step 3: Implement** `resolve_project`, `bind_project`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): project resolution and per-session binding, without the xiulian fallback`.

### Task 3: Per-session state

**Files:**
- Create: `hook/state.py`
- Test: `tests/hook/test_state.py`

**Interfaces:**
- Produces: `class SessionState` used as a context manager: `with SessionState.open(store: Path, harness: str, session_id: str) as st:` exposes `st.data: dict` and saves on exit under an exclusive `fcntl.flock` on `<store>/hook-state/<harness>-<session_id>.json.lock`. Keys used by later tasks: `directive_sent: bool`, `injections: int`, `entry_counts: dict[str, int]`, `skips_logged: list[str]`, `query_cache: dict[str, {"ts": float, "hits": list}]`, `pending_application: list[dict]`, `unshown_notices: list[str]`. Session ids are sanitised to `[A-Za-z0-9_.-]`. Callers hold the context only to read what they need and to commit results: the pattern is read under the lock, release, do the search or LLM call, then reopen to commit (Global Constraints).

- [ ] **Step 1: Write the failing tests** `test_state_roundtrip`, `test_state_sanitises_session_id`, `test_state_lock_not_held_by_slow_work` (one thread holds a 3 s fake critic call between two short `open` blocks; another thread's `open` returns in under 0.5 s), and

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
- Test: `tests/hook/test_boot_context.py`, `tests/hook/test_directive.py`, `tests/hook/test_parity_plugins.py` (Node driver, skipped without `node`, as in `opencode_cc_mem/tests/test_claim_critic_plugin.py`, which is `../../tests/test_claim_critic_plugin.py` from the package directory)

**Interfaces:**
- Consumes: `ProjectPaths`, `SessionState`.
- Produces: `MAX_PAYLOAD_CHARS = 256_000` (a prefix length; the suffix is added after it); `build_boot_payload(paths: ProjectPaths) -> str`; `DIRECTIVE: str` (verbatim from the plugin, leading `"\n\n"` included); `retrieval_directive(paths, state, session_id) -> str | None` (returns the directive once per session, appends `{ts, sessionID, action: "inject"}` to `<store>/auto-retrieval.jsonl`; `None` when `MAGNOLIA_AUTORETRIEVE` is `0/off/false/no`).

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

- [ ] **Step 1: Write the failing tests** `test_doorbell_writes_letter_and_rewrites`, `test_doorbell_bare_mention_untouched`, `test_doorbell_disabled_agent_ignored` (uses a non-default agent from `projects/<x>/agent.json`, since `enabled: false` does not remove the defaults, `magnolia-agent-doorbell.ts:56-71`), `test_doorbell_kill_switch`; parity `test_doorbell_matches_plugin` (same input text through the v2 prompt hook under Node; compare rewritten text and letter body, ignoring the timestamp line).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): doorbell letters`.

### Task 6: Action-time retrieval

**Files:**
- Modify: `mcp-servers/compchem-memory/src/compchem_memory/quick_search.py:36-87` — extract `search(query: str, project_dir: str, k: int = 5) -> list[dict]` (the dicts `main()` prints today, fallback gist included); `main()` calls it. The hook calls it with `k=MAX_HITS`, as the plugin does (`magnolia-action-retrieval.ts:242-245`).
- Create: `hook/action_retrieval.py`
- Test: `tests/hook/test_action_retrieval.py`; the existing quick_search test (`tests/test_retrieval_readonly.py:39-57`) stays green.

**Interfaces:**
- Consumes: `ToolCall`, `ProjectPaths`, `SessionState`, `quick_search.search`, `surfacing.log_surfacing`.
- Produces:
  - `@dataclass ActionNote(text: str, sources_by_store: dict[Path, list[dict]])`: the block to show, and the surfacing rows it would justify, grouped by the store that served each hit.
  - `commit_exposure(note: ActionNote, session_id: str) -> None`: calls `surfacing.log_surfacing(store, sources, channel="action_memory", session_id=...)` once per serving store. The dispatcher (Task 9) calls it only after the adapter reports that it emitted `tool_note`; a dropped note leaves no exposure row.
  - `ACTION_TOOLS: frozenset[str]` in canonical form: `compchem-tools.run_shell`, `compchem-tools.submit_job`, `submit_job`, `write`, `edit`, `compchem-tools.gnina_dock`, `compchem-tools.gromacs_run`, `compchem-tools.gromacs_setup`, `compchem-tools.haddock3_run`, `compchem-tools.gaussian_run`, `compchem-tools.orca_run`, `compchem-tools.xtb_optimize`, `compchem-tools.xtb_singlepoint`, `compchem-tools.preprocess_pdb`, `compchem-tools.run_acpype` (canonical key = `f"{server}.{name}"` or bare `name`).
  - `args_to_query(args: object) -> str` (string leaves, depth ≤ 2, each < 2000 chars, joined, cut at 400).
  - `on_post_tool(event: HookEvent, paths: ProjectPaths, state: SessionState, deadline: float) -> ActionNote | None`; `ActionNote.text` is the block the plugin prepends today (`[Magnolia · action-memory] N past learning(s) matched this action (tool: <short>). ...`, `magnolia-action-retrieval.ts:351-363`).
  - `probe_application(paths, state, session_id: str, reply_text: str) -> None` (the plugin's lexical probe: an entry counts as applied when at least `min(2, token_count)` of its tokens, and at least one, appear in the reply; rows `event: "application"`, `magnolia-action-retrieval.ts:260-290`).

The search runs at post-tool time (one process per event cannot keep the plugin's pre-tool promise). `deadline` is an absolute `time.monotonic()` value set by the CLI at process start (Task 9); the search gets what remains of 2.5 s. On timeout log `{skipped: "timeout"}` and return `None`; the CLI then ends the process without waiting for the search (Task 9). Each hit is normalised before logging: `id = Path(hit["path"]).name`, serving store = the `.magnolia` directory holding `hit["path"]` (as `magnolia-action-retrieval.ts:384-405` does). The plugin writes two kinds of telemetry and P1 keeps both: a coarse call-outcome row per action call and a detailed injection row (`:322-383`). Deliberate change: the query cache moves from the plugin instance to `state.data["query_cache"]`, per session, with the 120 s TTL and a bound of 50 entries evicted oldest first, as the plugin's (`:203-206, 236-253`).

- [ ] **Step 1: Write the failing tests**: `test_args_to_query_depth_and_cut`; `test_action_retrieval_injects_block` (fixture store with three entries, two above score 3, one with score exactly 3, one provisional staging hit; assert the complete block text against the plugin's, ledger rows of both kinds); `test_action_retrieval_latch_and_budget` (third time an entry is offered it is dropped; ninth injection logs `skipped: "budget"` once); `test_action_retrieval_zero_hits`; `test_action_cache_evicts_oldest_after_50`; `test_action_retrieval_ignores_non_action_tools`; `test_action_retrieval_without_project_is_silent`; `test_action_retrieval_kill_switch` (`MAGNOLIA_ACTION_RETRIEVE=0`); `test_action_retrieval_timeout_skips` (monkeypatch `quick_search.search` to sleep 3 s; assert `None` and a `timeout` row); `test_probe_application_positive_and_negative`; `test_action_surfacing_rows_keep_collegium_shape` (hits `staging/collegium_card_1.md` and a project entry: rows carry the exact non-empty ids `collegium_card_1.md`, the serving store of each, and pass a copy of the predicate in "Alignment with Collegium"; the copy names its source, `collegium@e64e411:src/collegium/harness/magnolia/exposure.py:99-141`, and the fixture follows `tests/test_harness_magnolia_exposure.py:35-89` there); `test_no_exposure_before_commit` (`on_post_tool` alone writes no `surfacing.jsonl` row).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `quick_search.search`, then `action_retrieval.py`.
- [ ] **Step 4: Run** `python -m pytest -q tests/hook/test_action_retrieval.py` and `python -m pytest -q tests/test_retrieval_readonly.py` → PASS.
- [ ] **Step 5: Commit** `feat(hook): action-time retrieval in Python`.

### Task 7: Magnolia transcript and session mapping

**Files:**
- Create: `hook/transcript.py`, `hook/sessions.py`
- Test: `tests/hook/test_transcript.py`, `tests/hook/test_sessions.py`

**Interfaces:**
- Produces:
  - `transcript_path(store: Path, harness: str, session_id: str) -> Path` = `<store>/transcripts/<harness>/<session_id>.jsonl`
  - `append_event(store: Path, event: HookEvent) -> None`: rows `{schema_version, ts, kind, turn_id, agent, ...}`; `turn_id` and `agent` (`{id, type}` or `null` for the main agent) are on every row, `null` when the client did not give them, never guessed. `kind` in `prompt` (`text`), `tool` (`phase` = `pre` or `post`, `name`, `call_id`, `input`, and on `post` `output`, `is_error`), `assistant` (`text`), `agent-start`, `agent-end`, `session-start`, `session-end`.
  - Order of treatment, fixed: scrub first, then cut. The tool input is serialised to JSON and passed whole through `opencode_ingest.scrub_secrets` (key-aware JSON-field redaction, then `_SECRET_PATTERNS`, `opencode_ingest.py:30-63`); outputs and texts are passed through `scrub_secrets` before any cut; the unscrubbed value is never written. Then cuts: ordinary tool outputs at 2000 characters; outputs of the four evidence tools (`memory_get_context`, `memory_get_run_history`, `memory_search`, `memory_scan_headers`) kept whole, since the critic reads them uncapped (`claim-critic.ts:55-68`); NUL bytes removed. Deliberate change: inputs are stored up to 4000 characters of their JSON, with a `truncated: true` field; the plugin's trace had no input cap (`claim-critic.ts:265`). One `os.write` per row under `fcntl.flock`.
  - `read_turns(path: Path) -> list[dict]` (rows in order, malformed lines skipped).
  - `record_session(store: Path, harness: str, session_id: str, cwd: str, transcript_ref: str | None) -> bool` writes `{ts, harness, session_id, directory, transcript_ref}` once per session to `<store>/agent-sessions.jsonl`; returns whether it wrote.
  - `iter_sessions(store: Path) -> Iterator[dict]` yields rows of `agent-sessions.jsonl` plus legacy `opencode-sessions.jsonl` rows mapped to `{harness: "opencode", session_id: <opencode_session_id>, ...}`.

- [ ] **Step 1: Write the failing tests**: `test_transcript_rows_by_kind`, `test_transcript_truncates_and_escapes` (5 MB output with `\x00`; row parses, output length 2000), `test_transcript_concurrent_appends` (8 threads × 50 rows → 400 parseable rows), `test_transcript_scrubs_secrets` (a nested `{"db": {"password": "hunter2-plain"}}` input whose value has no token prefix, an `OPENAI_API_KEY=sk-…` assignment, a bearer token in an output, and a secret straddling character 2000 of an output: none appears in the row), `test_transcript_keeps_evidence_tool_outputs_whole` (a 10 000-character `memory_search` output survives), `test_transcript_phase_and_agent_on_every_row`, `test_record_session_once`, `test_iter_sessions_merges_legacy`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(hook): Magnolia transcript and agent-sessions mapping`.

### Task 8: Critic in Python on the shared LLM client

**Files:**
- Modify: `mcp-servers/compchem-memory/src/compchem_memory/llm.py` — add `@dataclass Route(provider, model, url, key, schema)`, `@dataclass CallResult(text: str | None, finish_reason: str | None, resolved_model: str | None, error: str | None, status: int | None)`, `resolve_route(role: Literal["memory", "critic"]) -> Route | None` and `call_route(route, system: str, user: str, *, max_tokens: int, temperature: float | None = None, json_mode: bool = False, disable_thinking: bool = False) -> CallResult` (never raises; HTTP status and error text go in the result, as the critic logs them today, `claim-critic.ts:196-212, 274-280`). Role `memory` reproduces today's routing; Anthropic is resolved explicitly (the memory path uses the Anthropic SDK and `_get_base_url` has no Anthropic entry, `llm.py:103-119, 268-275, 365-383`), not through `_get_base_url("anthropic")`. Role `critic` reads `MAGNOLIA_CRITIC_{PROVIDER,MODEL,API_KEY,BASE_URL}` with the same seven providers, key precedence, defaults and order as `claim-critic.ts`, and its request defaults (`max_tokens=500`, `temperature=0`, JSON mode and thinking settings, `:163-203`). `_resolve_call()`, `_get_api_key(provider)`, `_get_base_url(provider)` and `call_llm` (signature, return values including `return_finish_reason`, resolved-model telemetry, `llm.py:309-362`) stay unchanged: Collegium and the provider tests call them.
- Create: `hook/critic.py`, `hook/notices.py`
- Test: `tests/hook/test_critic.py`, `tests/hook/test_notices.py`; existing `tests/test_llm_providers.py` stays green.

**Interfaces:**
- Consumes: `read_turns`, `Route`, `CallResult`, `call_route`, `drain_job_notices`.
- Produces:
  - `build_trace(rows: list[dict], agent_id: str | None) -> tuple[str, str]` → `(report, trace_text)`: rows are restricted to the agent being judged (`None` = main agent); report = that agent's assistant text after its last prompt (or its last `agent-start` for a subagent); trace = one item per terminal tool call (a `post` row; a `pre` row with no `post` appears once, marked unfinished); current-turn outputs cut at 2000 characters, earlier turns at 500, the four evidence tools uncapped; JSON; whole trace cut at 80 000 with `"\n…(truncated)"`.
  - `run_critic(paths: ProjectPaths, harness: str, session_id: str, agent_id: str | None) -> None`: no-op unless `MAGNOLIA_CRITIC` is non-empty (as the plugin: `"0"` still enables it) and the report has ≥ 200 characters; writes the same verdict row as the plugin to `<store>/claim-critic/<session_id>.jsonl` (plus `harness`, `agent`), errors with status and text to `errors.jsonl`; a flagged verdict appends `⚠ (<severity>) <why[:140]>` to `unshown_notices` in a short state commit after the call.
  - `pending_notices(paths, state) -> str | None`: drains job notices with `drain_job_notices(str(paths.project_dir), limit=10)` unless `MAGNOLIA_JOB_NOTIFY == "0"` (the plugin's batch of 10; the function's default is 20), format `<icon> <tool> finished (<state>) — run …<last 24 of run_id>`, cut at 160, then `unshown_notices`, one per line.

- [ ] **Step 1: Write the failing tests**: `test_resolve_route_critic_matches_plugin` (parametrised over all seven providers, Kimi and OpenRouter included, plus the eight cases of `../../tests/test_claim_critic_plugin.py`, comparing `Route.url/model/key`); `test_call_route_shapes` (monkeypatched `httpx.post`: Anthropic and OpenAI shapes, HTTP 401 with its text in `CallResult.error`, empty reply, `finish_reason == "length"`, echoed model in `resolved_model`); `test_memory_route_anthropic_explicit`; `test_legacy_llm_helpers_unchanged` (`_resolve_call`, `_get_api_key`, `_get_base_url` return what they return at acf96ee for the cases of `tests/test_llm_providers.py`); `test_critic_request_matches_plugin` (SYSTEM and user text, `max_tokens`, temperature, JSON and thinking settings, against the plugin's request under the Node driver); `test_critic_report_length_boundary` (199 characters: no call; 200: one call); `test_build_trace_caps` (an evidence output beyond character 2000, an ordinary current output, an earlier output, a long input; built from `append_event` → `read_turns` → `build_trace`); `test_build_trace_one_item_per_call` (a pre/post pair gives one item; an unmatched pre gives one unfinished item); `test_build_trace_parent_and_child_apart` (interleaved parent and subagent rows: each trace holds only its agent's calls); `test_run_critic_writes_verdict_and_notice`; `test_run_critic_logs_http_error`; `test_pending_notices_drains_ten` (eleven notices: ten now, one next time); `test_pending_notices_malformed_rows_and_switch`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `Route`, `CallResult`, `resolve_route`, `call_route`, then `critic.py`, `notices.py`.
- [ ] **Step 4: Run** `python -m pytest -q tests/hook/test_critic.py tests/hook/test_notices.py tests/test_llm_providers.py ../../tests/test_claim_critic_plugin.py` → PASS.
- [ ] **Step 5: Commit** `feat(hook): claim critic and notices on the shared LLM client`.

### Task 9: Dispatcher and CLI

**Files:**
- Create: `hook/dispatch.py`, `hook/__main__.py`, `hook/adapters/__init__.py`, `hook/adapters/json_adapter.py`
- Test: `tests/hook/test_dispatch.py`, `tests/hook/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `handle(event: HookEvent, env: Mapping[str, str], deadline: float) -> Handled`, with `@dataclass Handled(response: HookResponse, pending: ActionNote | None)`. It first calls `bind_project` (every event, so a client without session-start is bound on its first event, and `record_session` runs once on whichever event comes first). Routing: `session-start` → transcript, boot payload + directive as `context`; `prompt` → transcript, doorbell (`context` = "The following @mentions were already sent to helper agents; do not act on them: …" when letters were written), directive if not yet sent, `pending_notices` as `notice`; `pre-tool` → transcript only; `post-tool` → transcript, action retrieval as `tool_note` (its exposure left in `pending`), `pending_notices`; `turn-end` → transcript (`assistant_text`), `probe_application`, `run_critic` for the event's agent; `agent-start`, `agent-end`, `session-end` → transcript.
  - Adapter protocol in `adapters/__init__.py`: `parse(raw: dict, event: str) -> HookEvent`, `render(resp: HookResponse, event: HookEvent) -> Rendered`, with `@dataclass Rendered(stdout: str, stderr: str, exit_code: int, emitted: frozenset[str])`; `emitted` names the response fields the client will actually receive. How a client blocks (exit code, JSON decision) is the adapter's, not the core's. `ADAPTERS = {"json": json_adapter}` (Plan 2 adds `claude`, `codex`, `vibe`); the `json` adapter emits every field and blocks with exit 2.
  - CLI: `python -m compchem_memory.hook <harness> <event>` sets `deadline = time.monotonic() + 2.5` for `post-tool` (8.0 for other events) at process start, reads stdin, calls `handle`, then `render`; calls `commit_exposure(pending, session_id)` only when `"tool_note" in rendered.emitted`; writes `stdout`/`stderr`, then leaves with `os._exit(rendered.exit_code)` after flushing, so that a search thread still running cannot hold the process. Any exception is written to `<store>/hook-errors.jsonl` (or `<state_home>/hook-errors.jsonl` when no project resolves) and the CLI prints nothing and exits 0.

- [ ] **Step 1: Write the failing tests**: `test_dispatch_session_start_context`, `test_dispatch_prompt_doorbell_context`, `test_dispatch_post_tool_note`, `test_dispatch_turn_end_runs_critic` (critic monkeypatched), `test_dispatch_binds_on_first_event_of_any_kind`, `test_dropped_tool_note_leaves_no_exposure` (an adapter whose `emitted` lacks `tool_note`: no `surfacing.jsonl` row), `test_block_rendered_by_adapter` (json adapter: exit 2, reason on stderr), `test_core_without_any_model_credential` (no `*_API_KEY` in the environment: session-start, prompt, post-tool and turn-end all succeed, the critic writes nothing), `test_cli_post_tool_exits_within_deadline` (subprocess; `quick_search.search` patched through an environment hook to sleep 10 s; the process exits in under 3.0 s with a `timeout` row), and

```python
@pytest.mark.parametrize("stdin", ["", "not json", "{}", '{"event":"prompt"}',
                                   '{"harness":"json","event":"post-tool","session_id":"s","cwd":"/nope","tool":{"raw_name":"x","output":[1,2]}}'])
def test_cli_garbage_payloads_exit_zero(stdin):
    proc = subprocess.run([sys.executable, "-m", "compchem_memory.hook", "json", "post-tool"],
                          input=stdin, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and proc.stdout == ""
```

  plus `test_cli_cold_start_under_budget` (median of 5 runs each of `session-start` and of `post-tool` on an action tool, on a fixture workspace, < 1.0 s; record both measured values in the commit message). The test of the deadline needs a way to slow the search from outside the process; use an environment variable read only under `MAGNOLIA_HOOK_TEST=1`, documented in the module.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; keep imports in `__main__` lazy (import behaviour modules inside `handle` branches).
- [ ] **Step 4: Run** `python -m pytest -q tests/hook` → PASS (parity cases run, not skipped), then the full suite `python -m pytest -q tests` → green, counts recorded, and `python -m pytest -q ../../tests/test_claim_critic_plugin.py ../../tests/test_campaign_reviewers.py` → PASS.
- [ ] **Step 5: Commit** `feat(hook): dispatcher and CLI entry point`.

### Task 10: Hand-back for review

**Files:**
- Modify: `opencode_cc_mem/docs/harness-neutral-agents-design.md` — section 9: P0 and P1 marked done with commit ids; section 5 updated from the probe report.
- Modify: `docs/harness-adaptation.md` — one paragraph pointing to the hook core and stating that OpenCode still uses the TypeScript plugins, and that distillation and handover do not yet read the new transcripts (design section 6.3 waits for Plan 2).

- [ ] **Step 1: Update both documents.**
- [ ] **Step 2: Run** `git diff --check` → no output; full suite as in Task 9 → PASS.
- [ ] **Step 3: Commit** `docs: record P0 and P1 of the harness-neutral design`. Nothing is pushed; the branch goes to Tao for review (design section 9).
