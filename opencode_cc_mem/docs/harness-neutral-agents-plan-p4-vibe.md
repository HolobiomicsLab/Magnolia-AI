# Harness-neutral Magnolia — Plan 4: Mistral Vibe acceptance

> **For agentic workers:** implement task-by-task with TDD. Steps use checkbox (`- [ ]`) syntax. Accepted by the lead on 17 Nov 2026; Task 6 is the later live protocol.

**Goal:** make Mistral Vibe 3.1.2 eligible for capability-gated worker, reviewer, helper and evaluator runs on the member's own Mistral account, then obtain enough live evidence to replace each refused capability row with a current, accepted row without weakening trust, role isolation, usage accounting or the strict hook boundary.

**Architecture:**
- **Render and bind first.** Reuse the run-owned `.vibe/config.toml` / `.vibe/hooks.toml`, normalized launch identity and native-artifact drift checks. Add only Vibe installation metadata discovery and role-specific tool policy; do not build another launcher or provider stack (`opencode_cc_mem/campaign_runner/launch.py:535`, `opencode_cc_mem/campaign_runner/executors/vibe.py:426`).
- **Mistral account only.** Keep `active_model = context.model`, telemetry disabled by the effective child environment, and the existing export/fallback parser. A missing or different effective model is never accepted (`opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/harness/render_vibe.py:85`, `opencode_cc_mem/campaign_runner/executors/vibe.py:52`, `opencode_cc_mem/campaign_runner/executors/vibe.py:320`). An OpenRouter-rendered provider is a possible later design item only.
- **One memory path.** Use the existing project-bound loopback HTTP service, static bearer environment reference and lifecycle lease only when the projected role has `vibe_memory.accepted = true` (`opencode_cc_mem/campaign_runner/launch.py:379`, `opencode_cc_mem/campaign_runner/launch.py:1304`, `opencode_cc_mem/campaign_runner/launch.py:1430`).
- **Capability table is the gate.** Remove the two unconditional Vibe-worker refusals, retain the accepted-row requirement, and give only a worker edit/write tools. The strict `pre_tool` hook remains the security boundary; native disabled-tool settings are defense in depth (`opencode_cc_mem/campaign_runner/executors/registry.py:147`, `opencode_cc_mem/campaign_runner/executors/vibe.py:195`, `opencode_cc_mem/campaign_runner/executors/vibe.py:437`).
- **Evidence remains separate from candidacy.** Offline candidate projections permit dry runs but never observe a cell or accept a real row. Changed identities are recorded old -> new and require live re-observation (`opencode_cc_mem/campaign_runner/executors/capabilities.py:73`).

**Spec:** `opencode_cc_mem/docs/harness-neutral-agents-design.md` sections 2, 3, 5, 8 and 9; Plan 3 format and contracts in `opencode_cc_mem/docs/harness-neutral-agents-plan-p3.md`; Vibe status and evidence expectations in `opencode_cc_mem/docs/harness-acceptance-p3-2026-11.md:106`.

**Gap report:** `task-15-live4-report.md` (Plan 3 live pass 4 report, kept in the development hub). Task 1 closes technical gap 1 (version metadata); Task 3 closes gap 2 (D3 candidacy); Task 4 closes gap 3 (D4); Task 2 closes gaps 4 and 5 by ruling out a provider-key route and hardening the Mistral-account environment/model proof. Task 6 closes the original R1 trust blocker only through the owner's explicit persisted-trust action (`task-15-live4-report.md:20`, `task-15-live4-report.md:111`).

**Base:** branch `feat/harness-vibe-p4`, base `dd34608`. Reconfirm the base before implementation. All paths below are relative to `opencode_cc_mem/` unless absolute; `src/compchem_memory/` means `mcp-servers/compchem-memory/src/compchem_memory/`.

## Global Constraints

- **Lead rulings.** R-a Mistral subscription route and exact model guard; R-b dist-info metadata; R-c D3 loopback only behind `vibe_memory`; R-d accepted-row worker with confined edit/write and no shell/process tools; R-e persisted trust only. Do not reopen these rulings.
- **Credentials and environment.** No provider API key, bearer secret or subscription credential may enter the Vibe client environment or a rendered artifact. Never set `VIBE_HOME`, `CLAUDE_CONFIG_DIR` or `CODEX_HOME`; never pass `--trust`, permission/hook-trust bypasses or unsafe sandbox flags. Vibe `--auto-approve` remains limited to the proved noninteractive disabled-tool/strict-hook contract. Never read/list/write `~/.vibe/` in offline work; do not copy subscription auth files or use keychain entries.
- **Provider routing.** Do not add OpenRouter, custom provider blocks, inline keys or a custom `api_key_env_var` route. Remove inherited endpoint overrides as well as keys from the Vibe child so environment configuration cannot silently select another provider. Memory-service bearer injection is the sole secret exception and must remain run-owned, mode `0600`, absent from files and logs.
- **Enforcement and trust.** Missing/untrusted/stale evidence, artifact drift, unknown tools or unresolved targets fail closed. Reviewer/helper/evaluator keep edit/write, shell and process tools disabled. Worker edit/write is confined to `work_dir` by the required strict `pre_tool`; do not treat CLI disablement as the security boundary. Preserve the existing persisted nearest-ancestor trust precheck and no-`--trust` behavior (`campaign_runner/executors/vibe.py:106`).
- **Tests and state.** Use only synthetic projects, sentinels, clients and candidate tables. No Vibe process, real model call, non-loopback network or paid call in Tasks 1–5. Never read/write the real `~/.local/state/magnolia`, `~/.config/magnolia`, `~/.magnolia` or `~/.codex/config.toml`; use temporary homes. No Git state changes are part of these tasks.
- **Capabilities.** `campaign_runner/executors/capabilities-p3.json` remains refused/unobserved in Tasks 1–5. Candidate accepted values are in-memory/test copies marked unobserved. Any changed `native_files`, `policy`, `settings`, `mcp`, `required_env` or other identity component is reported old -> new; stale rows remain unaccepted (`campaign_runner/executors/capabilities.py:73`).
- **TDD.** Write each named test first, run its exact selection and record the RED count, implement, rerun GREEN, then run covering suites. Existing tests change only in files named by the task. Preserve all non-Vibe behavior and current Claude/Codex/OpenCode rows.
- **Verification commands.** Use `PY=<the memory-suite virtualenv python>`, `WT=<worktree>/opencode_cc_mem`, `export PYTHONDONTWRITEBYTECODE=1`, and `export PYTHONPATH="$WT:$WT/mcp-servers/compchem-memory/src:$WT/mcp-servers/compchem-tools/src"`. Every pytest command adds `-p no:cacheprovider --basetemp=<fresh directory under /private/tmp>`. Also run campaign and memory suites from an rsync copy at another absolute path. Record known baseline/sandbox failures rather than converting them to passes.
- **Scope.** No Magnolia-rendered OpenRouter provider, upstream Vibe patch, new client, real-secret probe or automatic row promotion. No implementation may infer acceptance from a dry run, stub or synthetic fixture.

## Review Focus

1. **Version confusion:** a Vibe executable is accepted only when its own environment yields exactly one well-formed `mistral-vibe` dist-info `Version`; zero, multiple, malformed or unreadable candidates refuse without launching Vibe.
2. **Silent provider/model substitution:** arbitrary inherited `*_API_KEY`, provider endpoint overrides and telemetry-on values cannot reach Vibe; export success with no `active_model` or a fallback model becomes `failure`/`model_mismatch`, never a usable result.
3. **Memory leakage or premature acquisition:** without accepted `vibe_memory`, no service starts, MCP entry appears or token is read; with candidacy, loopback URL, bearer env and normalized identity work, and every refusal/exit/reviewer-retry path releases exactly once.
4. **Worker boundary regression:** an accepted worker can edit/write only inside its work tree while `file_system.bash`, `process.start` and `process.write` stay disabled; every other role remains read-only and strict-hook failures still deny before execution.
5. **False acceptance and owner-state drift:** synthetic/candidate evidence cannot clear a real row; changed identities stay stale; live steps stop on missing trust, model mismatch, invocation-budget exhaustion, leaked sentinel, leftover service/process or changed owner state.

## Execution phases and dependencies

**A — launch eligibility:** Tasks 1 and 2, independently implementable after test fixtures are shared. **B — gated features:** Task 3 after Tasks 1–2; Task 4 after Tasks 1–2 and the D3 regression contract. **C — integrated offline proof:** Task 5 after Tasks 1–4. **D — live acceptance:** Task 6 only after the lead accepts the offline evidence and separately authorizes model calls and the owner trust action. Tasks 1–5 make no capability-row acceptance change.

### Task 1: Bind Vibe's installed distribution version — `cheap`

**Closes:** live-4 technical gap 1; R-b.

**Files:** modify `campaign_runner/launch.py:778`; extend `tests/test_executor_launch.py` with Vibe metadata fixtures near the existing installed-client tests. No production table change.

**Interfaces:**
- Consume `_installed_client_version(executable: Path, *, invocation_dir: Path, launch_env: Mapping[str, str]) -> str`, with the resolved Vibe executable already available from `_resolved_client_executable()` (`campaign_runner/launch.py:545`).
- Produce the same return contract for Claude/Codex plus a Vibe dist-info branch. Add private `_vibe_distribution_version(executable: Path) -> str`; no caller changes.
- Current installed shape to support: resolved `.../mistral-vibe/bin/vibe`, sibling `lib/python<major.minor>/site-packages/mistral_vibe-<version>.dist-info/METADATA`, with `Name: mistral-vibe` and `Version: 3.1.2`. Do not assume the current `python3.14` directory name in the algorithm.

- [ ] **Step 1: Write RED tests.** Use a fake `<env>/bin/vibe`; never invoke it. Add `test_installed_vibe_version_reads_owning_dist_info`, `test_installed_vibe_version_ignores_other_environment_metadata`, and parametrized `test_installed_vibe_version_refuses_missing_ambiguous_or_malformed_metadata` for zero directories, two matching distributions, wrong `Name`, missing/duplicate `Version`, empty version and unreadable `METADATA`.

```python
assert _installed_client_version(binary, invocation_dir=run, launch_env={}) == "3.1.2"
with pytest.raises(ExecutorRefused, match="Vibe.*(metadata|dist-info).*(install|reinstall|repair)"):
    _installed_client_version(binary, invocation_dir=run, launch_env={})
# A second env containing another version must not affect the first result.
```

- [ ] **Step 2: Observe RED and implement.** Run the new selection; expect failures because the current function only recognizes Codex/Claude and raises at `campaign_runner/launch.py:808`. Implement `_vibe_distribution_version(executable: Path) -> str`: resolve the executable strictly, search only its owning environment's `lib/python*/site-packages/mistral_vibe-*.dist-info/METADATA`, parse RFC822 headers with `email.parser`, require exactly one valid distribution, and raise `ExecutorRefused` naming the resolved binary, candidate count and repair action for every other case. Detect the Vibe layout without changing Claude/Codex precedence or running the client.
- [ ] **Step 3: Verify and bind.** Rerun the new selection plus `tests/test_executor_launch.py`; expect new tests GREEN and no old-version regressions. Add one production-shaped fixture assertion that `_normalized_launch_identity()` includes `client_version == "3.1.2"` without launching Vibe.

**Commands:**

```bash
cd "$WT" && "$PY" -m pytest -q tests/test_executor_launch.py -k 'installed_vibe_version or normalized_launch_identity' -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t1-red
cd "$WT" && "$PY" -m pytest -q tests/test_executor_launch.py -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t1-green
```

**Expected:** initial new tests fail with version metadata unavailable; final selection passes and Vibe normalized identity is computable offline. **Identity impact:** no digest should change solely by reading metadata; previously uncomputable Vibe `launch_identity.components.client_version` becomes `3.1.2`. Report candidate identities that become computable old `(unavailable)` -> new full digest; do not edit observed row values.

### Task 2: Pin the Mistral-account route and sanitize Vibe's child environment — `hard`

**Closes:** live-4 gaps 4 and 5 under R-a; preserves R-e. The existing guard is retained, not redesigned (`campaign_runner/executors/vibe.py:323`; tests at `tests/test_executor_vibe.py:323` and `tests/test_executor_vibe.py:349`).

**Files:** modify `campaign_runner/launch.py:1579`, `campaign_runner/executors/vibe.py:195` only if validation needs a shared helper; extend `tests/test_executor_launch.py`, `tests/test_executor_vibe.py`. Do not change renderer model configuration or trust code.

**Interfaces:**
- Consume `ExecContext.route.model`, inherited `context.environment`, `context.renderer_env`, run-owned memory route, and `_effective_environment(...) -> dict[str, str]` (`campaign_runner/launch.py:1579`).
- Produce a Vibe-only inherited/rendered environment allowlist containing process basics and the explicitly permitted test stub setting, followed by Magnolia's explicit environment. Add `VIBE_BASE_ENV: frozenset[str]` and `_vibe_inherited_environment(context: ExecContext, inherited: Mapping[str, str]) -> dict[str, str]`; no provider names, endpoint selectors or secret values are returned.
- Continue producing `active_model` in rendered TOML (`src/compchem_memory/harness/render_vibe.py:95`), `ParsedVibeRun.reported_model` from `config.active_model`/fallback notices (`campaign_runner/executors/vibe.py:72`), and `RunOutcome("model_mismatch")` for inequality (`campaign_runner/executors/vibe.py:332`). No new provider interface.

- [ ] **Step 1: Write RED environment/model tests.** Extend the stub's recorded environment to include `VENDOR_SPECIFIC_API_KEY`, `OPENAI_BASE_URL`, `MISTRAL_BASE_URL`, `HTTP_PROXY` and `VIBE_STUB_CONFIG` (test-only transport). Replace the obsolete expectation that arbitrary provider keys pass (`tests/test_executor_vibe.py:36`). Add `test_vibe_child_env_allows_only_mistral_account_process_basics`, `test_vibe_renderer_env_cannot_inject_provider_or_endpoint`, `test_vibe_memory_token_is_only_secret_in_child_env`, and `test_vibe_missing_or_mismatched_effective_model_is_not_accepted`.

```python
assert "VENDOR_SPECIFIC_API_KEY" not in prepared.env
assert "OPENAI_BASE_URL" not in prepared.env
assert "MISTRAL_API_KEY" not in prepared.env
assert prepared.env["VIBE_ENABLE_TELEMETRY"] == "false"
assert prepared.env["MAGNOLIA_MEMORY_SERVICE_TOKEN"] == "service-bearer-sentinel"
assert result.outcome.kind == "model_mismatch"
assert "model_mismatch" in [item["kind"] for item in result.errors]
```

- [ ] **Step 2: Prove current model behavior before editing.** Keep `test_vibe_real_export_selected_model_and_fallback_notice` and the success/fallback/missing-export matrix. Add missing-`active_model` and successful-process fallback cases asserting `failure`/`model_unverified` and `model_mismatch`, with no accepted session result and exactly one usage receipt. These may already pass; record them as characterization, not RED implementation work.
- [ ] **Step 3: Observe environment RED and implement.** Run new environment tests; expect arbitrary key and endpoint leakage because the current branch uses name-based removal at `campaign_runner/launch.py:1675`. Implement Vibe-only allowlisting before `_explicit_environment`; preserve `PATH`, locale/terminal/tmp basics required by Vibe, and `VIBE_STUB_CONFIG` solely for the existing test double. Do not forward proxy or endpoint overrides. Let `_explicit_environment` add Magnolia/hook variables, the loopback bearer token and `VIBE_ENABLE_TELEMETRY=false` afterward (`campaign_runner/launch.py:1748`). Preserve all Claude/Codex branches and memory-service launcher environment behavior.
- [ ] **Step 4: Verify route and trust invariants.** Rerun both named files. Assert no rendered `providers` block or OpenRouter configuration, `active_model == route.model`, telemetry false, `VIBE_HOME`/`--trust` absent, persisted trust still called before preparation (`campaign_runner/executors/registry.py:258`), and model mismatch does not produce an accepted result.

**Commands:**

```bash
cd "$WT" && "$PY" -m pytest -q tests/test_executor_launch.py tests/test_executor_vibe.py -k 'vibe_child_env or vibe_renderer_env or vibe_memory_token or effective_model' -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t2-red
cd "$WT" && "$PY" -m pytest -q tests/test_executor_launch.py tests/test_executor_vibe.py -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t2-green
```

**Expected:** RED demonstrates unlisted provider-key/endpoint leakage; GREEN proves only the Mistral subscription route, telemetry-off and model mismatch refusal. **Identity impact:** `required_env` may change old -> new if the bound environment-name set changes; `native_files`, `policy` and `settings` stay unchanged. The selected model is not a new identity field; record the exact model id in the candidate/live evidence. Any changed identity requires re-observation.

### Task 3: Prove accepted-cell D3 memory candidacy and lifecycle — `cheap`

**Closes:** live-4 gap 2; R-c. The implementation already exists; this task closes coverage/candidate integration rather than adding a second service path (`campaign_runner/launch.py:156`, `campaign_runner/launch.py:1272`, `campaign_runner/launch.py:1430`).

**Files:** extend `tests/test_executor_launch.py`, `tests/test_executor_vibe.py`, and memory `mcp-servers/compchem-memory/tests/harness/test_memory_service.py` only if lifecycle coverage is missing. Production changes are limited to defects exposed by the new tests in `campaign_runner/launch.py:379` or `campaign_runner/executors/vibe.py:214`; no service protocol rewrite.

**Interfaces:**
- Consume projected `role_capabilities["vibe"][role]["vibe_memory"] is True` (`campaign_runner/executors/capabilities.py:104`), `memory_service={"transport":"managed"}`, role memory allowlist, project/Python and test service handle.
- Produce `mcp.json.memory_service={transport,url,authorization_env}` with no token path, `.vibe/config.toml.mcp_servers=[{name,transport="http",url,auth={type="static",api_key_env}}]`, and child bearer from a mode-`0600` token file (`campaign_runner/launch.py:1430`, `campaign_runner/launch.py:1750`). Installed Vibe 3.1.2 supports those exact HTTP/auth fields (`vibe/core/config/models.py:225`, `vibe/core/config/models.py:273`, `vibe/core/config/models.py:424`).
- Retain `prepare_launch(context) -> PreparedLaunch`, executor `run()` release, reviewer `close()` after format repairs, and registry release on construction refusal (`campaign_runner/executors/registry.py:294`).

- [ ] **Step 1: Write a candidate dry-run matrix.** Add `test_vibe_memory_candidate_worker_dry_run_end_to_end`, `test_vibe_memory_unaccepted_cell_never_acquires_or_reads_token`, `test_vibe_memory_candidate_rejects_nonloopback_url`, `test_vibe_memory_candidate_token_requires_0600_and_never_enters_artifacts`, and `test_vibe_memory_identity_normalizes_ephemeral_url_but_binds_enabled_route`.

```python
candidate = replace(context, role_capabilities={"vibe": {"worker": {
    **accepted_worker_evidence, "vibe_memory": True,
}}})
launch = prepare_launch(candidate)
server = tomllib.loads((launch.invocation_dir / ".vibe/config.toml").read_text())["mcp_servers"][0]
assert server["url"].startswith("http://127.0.0.1:")
assert server["auth"]["api_key_env"] == "MAGNOLIA_MEMORY_SERVICE_TOKEN"
assert "service-bearer-sentinel" not in _artifact_text(launch)
assert acquired_count == 1 and released_count == 0  # before executor completion
```

- [ ] **Step 2: Cover release ownership.** Add `test_vibe_memory_released_once_after_worker_exit`, `test_vibe_memory_released_after_prepare_or_validation_refusal`, and `test_vibe_memory_reviewer_lease_survives_repairs_then_close_releases_once`. Reuse release tests at `tests/test_executor_vibe.py:271` and `tests/test_executor_vibe.py:291`; do not spawn a real service outside loopback. Assert unaccepted cells retain the current empty MCP behavior (`tests/test_executor_launch.py:1379`).
- [ ] **Step 3: Observe failures and make minimal fixes.** Run the matrix. Existing tests at `tests/test_executor_launch.py:1242`, `:1289`, `:1334`, `:1465` should remain green. If a candidate integration assertion fails, fix the existing gate/lifecycle at its source, not by adding another memory renderer or permitting non-loopback transport.
- [ ] **Step 4: Verify candidate versus real table.** Run named campaign and memory tests. Serialize the in-memory candidate with every promoted value marked `CANDIDATE-UNOBSERVED`; assert loading `capabilities-p3.json` still projects every Vibe role refused and `vibe_memory=False` (`campaign_runner/executors/capabilities.py:104`).

**Commands:**

```bash
cd "$WT" && "$PY" -m pytest -q tests/test_executor_launch.py tests/test_executor_vibe.py -k 'vibe_memory' -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t3-red
cd "$WT" && "$PY" -m pytest -q tests/test_executor_launch.py tests/test_executor_vibe.py mcp-servers/compchem-memory/tests/harness/test_memory_service.py -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t3-green
```

**Expected:** an accepted in-memory `vibe_memory` candidate acquires one loopback service, renders native HTTP/auth and run-owned bearer, normalizes ephemeral URL identity and releases on every terminal path; unaccepted cells do nothing. **Identity impact:** `mcp` and `native_files` change from empty-memory to memory-enabled candidate values old -> new; port/token/path bytes are normalized and the token never enters a digest payload. Real rows remain refused and unobserved.

### Task 4: Enable the capability-gated D4 worker with role-specific tools — `hard`

**Closes:** live-4 gap 3; R-d.

**Files:** modify `campaign_runner/executors/registry.py:147`, `campaign_runner/executors/vibe.py:195` and `:405`, `mcp-servers/compchem-memory/src/compchem_memory/harness/render_vibe.py:42` and `:85`; extend `tests/test_executor_vibe.py`, memory `mcp-servers/compchem-memory/tests/harness/test_role_launch.py` (or the existing Vibe native-renderer test module), and `tests/test_executor_capabilities.py` only for refusal projection assertions.

**Interfaces:**
- Consume an accepted current worker projection from `_capable(context,"vibe","worker")`; keep `executor_for(route: ResolvedRoute, role: str, context: ExecContext) -> WorkerExecutor | Any` (`campaign_runner/executors/registry.py:147`).
- Add `disabled_tools_for_role(role: str) -> tuple[str, ...]` in `render_vibe.py`: worker -> `("file_system.bash","process.start","process.write")`; reviewer/helper/evaluator -> existing `READ_ONLY_DISABLED_TOOLS` (`src/compchem_memory/harness/render_vibe.py:42`). Share this helper with `VibeExecutor._argv` so CLI and TOML cannot diverge.
- Preserve `VibeExecutor._validate_prepared() -> None`, strict `pre_tool` requirement and immutable artifact checks. Worker edit/write remains allowed by policy only under the run's `work_dir`; no new shell/process capability or trust flag.

- [ ] **Step 1: Replace the obsolete hard-refusal test.** Replace `test_vibe_worker_edits_require_D4_and_gate` (`tests/test_executor_vibe.py:312`) with `test_vibe_worker_requires_accepted_current_capability_row` and `test_vibe_accepted_worker_constructs_without_hard_D4_refusal`.

```python
with pytest.raises(ExecutorRefused, match="lacks accepted worker role capability"):
    executor_for(route, "worker", unaccepted_context)
executor = executor_for(route, "worker", accepted_context)
assert isinstance(executor, VibeExecutor)
assert executor.role == "worker"
```

- [ ] **Step 2: Write native/argv role-policy RED tests.** Add parametrized `test_vibe_disabled_tools_match_role_in_toml_and_argv`, `test_vibe_worker_retains_only_edit_and_write`, and `test_vibe_nonworker_roles_retain_full_read_only_deny_set`.

```python
worker = set(disabled_tools_for_role("worker"))
assert worker == {"file_system.bash", "process.start", "process.write"}
assert not {"file_system.edit", "file_system.write_file"} & worker
for role in ("reviewer", "helper", "evaluator"):
    assert set(disabled_tools_for_role(role)) == set(READ_ONLY_DISABLED_TOOLS)
# The same sets appear in TOML and repeated --disabled-tools argv values.
```

- [ ] **Step 3: Prove the strict boundary, not just flags.** Add `test_vibe_worker_work_edit_allowed_and_outside_edit_denied_by_pre_tool`, `test_vibe_worker_shell_and_process_tools_denied`, and `test_vibe_worker_strict_hook_failure_fails_closed_before_spawn`. Use the existing policy/native fixtures to assert a work-file edit decision is allowed, project/secret/alias/recursive/outside edits are denied, and the required strict hook remains immutable (`campaign_runner/executors/vibe.py:437`). These are synthetic hook decisions, not live evidence.
- [ ] **Step 4: Observe RED and implement.** Expect both unconditional refusals at `registry.py:161` and `vibe.py:196`, plus worker edit/write in argv at `vibe.py:420`. Delete only those hard-coded D4 branches; change `_validate_prepared` to accept all four named roles and reject others; replace the unconditional argv tuple with `disabled_tools_for_role(self.role)`; make worker rendering include process tools. Do not remove `_capable` or trust/project/identity checks.
- [ ] **Step 5: Verify all roles and drift.** Rerun campaign Vibe/capability tests and memory renderer/hook tests. Parametrize project drift over model, MCP, hook and each role's tool set; require refusal before spawn. Assert reviewer/helper/evaluator exact deny sets and worker confinement. Keep real rows refused.

**Commands:**

```bash
cd "$WT" && "$PY" -m pytest -q tests/test_executor_vibe.py tests/test_executor_capabilities.py mcp-servers/compchem-memory/tests/harness/test_role_launch.py -k 'vibe' -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t4-red
cd "$WT" && "$PY" -m pytest -q tests/test_executor_vibe.py tests/test_executor_capabilities.py mcp-servers/compchem-memory/tests/harness/test_role_launch.py mcp-servers/compchem-memory/tests/hook -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t4-green
```

**Expected:** accepted worker construction succeeds; unaccepted/stale rows still refuse; worker config/argv retain edit/write but deny bash/process; all other roles are read-only; strict hook and artifact drift fail closed. **Identity impact:** worker `native_files` changes old -> new (process deny entries); worker and nonworker policy/settings/native identities must be recomputed after sharing the helper, with any unexpected nonworker change treated as a regression. Do not alter recorded rows.

### Task 5: Integrated offline candidate and identity verification — `cheap`

**Closes:** integration precondition for all five gaps; proves Tasks 1–4 together without evidence promotion.

**Files:** create `tests/test_executor_vibe_p4_candidate.py`; extend `tests/test_executor_capabilities.py` only if the loader lacks a candidate-refusal assertion; create the task report under `.superpowers/sdd/vibe-p4/` during implementation. Production files change only for integration defects.

**Interfaces:** consume four in-memory candidate role projections, fake resolved Vibe installation, managed-memory handle and synthetic Vibe stub. Produce dry-run preparation/construction records, old -> new identity diffs and suite results; no `observed` cell or accepted production row.

- [ ] **Step 1: Write integrated tests.** Add `test_vibe_p4_candidate_worker_memory_dry_run`, `test_vibe_p4_candidate_readonly_roles_dry_run`, `test_vibe_p4_production_rows_remain_refused_and_unobserved`, and `test_vibe_p4_identity_diff_is_explicit_and_relocation_stable`.

```python
assert all(candidate[role]["accepted"] for role in ROLES)  # in-memory candidate only
assert all(not production[role]["accepted"] for role in ROLES)
assert all(row["cells"]["model_delivery"]["status"] != "observed" for row in vibe_rows)
assert relocated_worker_identity == source_worker_identity
assert diff["worker"]["native_files"]["old"] != diff["worker"]["native_files"]["new"]
```

- [ ] **Step 2: Run the focused matrix and inspect artifacts.** Candidate worker uses accepted D4 + `vibe_memory=True`; read-only roles use their accepted candidate rows and no memory where their policy has no tools. Check exact model, environment, hook, MCP, argv and identity bytes; run no Vibe binary.
- [ ] **Step 3: Run source and relocated suites.** Rsync campaign sources/tests and memory package to a fresh absolute path, then run complete campaign `tests` and memory `mcp-servers/compchem-memory/tests` in both locations with independent basetemps. Record counts, known baseline failures and sandbox loopback/signal limitations exactly. Remove invocation-local basetemps afterward.
- [ ] **Step 4: Write the implementation report.** Include changed files, RED/GREEN counts, every identity old -> new, candidate-versus-production assertions, source/relocated results and deviations. Do not write capability evidence or call anything accepted.

**Commands:**

```bash
cd "$WT" && "$PY" -m pytest -q tests/test_executor_vibe.py tests/test_executor_launch.py tests/test_executor_capabilities.py tests/test_executor_vibe_p4_candidate.py -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t5-focused
cd "$WT" && "$PY" -m pytest -q tests -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t5-source-campaign
cd "$WT" && "$PY" -m pytest -q mcp-servers/compchem-memory/tests -p no:cacheprovider --basetemp=/private/tmp/vibe-p4-t5-source-memory
# Repeat both complete suites from the fresh rsync copy with distinct /private/tmp basetemps.
```

**Expected:** focused candidate matrix passes; complete results are reported against the known campaign baseline and zero-failure memory baseline, with sandbox exceptions named; no production Vibe row changes. **Identity impact:** final report contains all changed component digests old -> new and confirms path-independent relocation; stale/unobserved evidence stays unaccepted.

### Task 6: Live Vibe capability gate and row-acceptance protocol — `hard`

**Purpose:** later live evidence collection only; no code in this task. It requires the accepted Task 5 report, the owner's standing authorization for Vibe model calls (cap 30) and the explicit owner trust action. It never uses an OpenRouter provider, provider API key, `--trust` or telemetry.

**Budget:** Vibe cap is **30 total physical model invocations; 5 already used; at most 25 remain**. A resume, repair or campaign retry consumes another invocation. Reserve **3** for continuity/repair and stop launching at 22 new calls if those reserves are untouched. Planned allocations below total **22**, with the remaining **3** held explicitly; do not borrow without a revised lead budget. No other client model call is needed. Ancillary loopback memory calls are counted separately and use only the approved sentinel provider.

| Ordered step | Physical Vibe invocation allocation | Gate/evidence produced |
|---|---:|---|
| 0. Offline precondition and baseline | 0 | Accepted Task 5 report; exact source revision; candidate table; fresh disposable Git project; planted `docs/notes.md`, `secret/.env`, `alias-env`, outside sentinel; no real secrets. |
| 1. Owner-state baseline and persisted trust | 0 | Owner grants trust for the resolved gate project in `~/.vibe/trusted_folders.toml` interactively, or explicitly authorizes a backed-up append. This is the only step that may touch that file; backup and byte-identical restore by `cmp` are required if Magnolia edits it. |
| 2. One model-delivery probe | 1 | Reviewer-shaped no-tools call proves the pinned Mistral-account model, export `config.active_model`, telemetry false, one governor receipt and no fallback. |
| 3. Reviewer role gate | 3 | Initial call plus at most two format/continuity calls; permitted read, denied secret/outside/shell/edit/write/process, valid review JSON, hooks and identity. |
| 4. D3 worker-memory gate | 4 | Worker memory readiness/first access, authenticated loopback MCP, record/search path, one continuation and one failure/cleanup probe; service released. |
| 5. D4 worker gate | 5 | Work-file edit/write succeeds; denied secret/alias/outside/project writes, bash/process and indirect/delegated attempts; resume, hook-crash/denial and identity recheck. |
| 6. Helper gate | 2 | Plain final response, cap, read-only/tool-denial evidence and one continuation/resume if supported. |
| 7. Evaluator gate | 2 | One-turn standalone structured/plain output, read-only/tool-denial evidence and one continuation/usage check if supported. |
| 8. Single-stage `worker-vibe-review` campaign | 4 | Worker + different-family reviewer campaign, one reviewer format repair at most and one continuity/resume at most; normal receipts, memory drain-once, allowed/denied traces and complete usage rows. |
| 9. Owner-state final check and row decision | 0 | Compare all baseline manifests/processes/services; restore trust backup if authorized; compute invocation total and per-cell evidence. |
| Reserve | 3 | Only for a named failed continuity/repair proof; every use is pre-recorded with reason and remaining budget. |

- [ ] **Step 0: Stop unless offline proof is complete.** Verify exact installed executable hash and dist-info version, all source/relocated suite results, candidate identities and current refused production rows. Stop on any identity mismatch, failing relevant test, unavailable model id, unapproved artifact or non-disposable project. Do not use `vibe --version` or launch Vibe for inspection.
- [ ] **Step 1a: Snapshot owner state without reading secrets.** Repeat live-4 checks (`task-15-live4-report.md:151`): manifests and hashes for `~/.local/state/magnolia`, `~/.config/magnolia`, `~/.magnolia`, `~/.magnolia/services`, `~/.local/share/opencode` and `~/.codex/config.toml`; free disk; existing Magnolia/Vibe/memory/compchem processes; owner PID 61561 status; existing services/ports. Do not read subscription credential contents or alter pre-existing processes.
- [ ] **Step 1b: Require normal persisted trust before any model call.** The owner performs the trust grant now. Run the production read-only precheck against the resolved gate project; require accepted nearest-ancestor trust and no session-only trust. **Stop immediately** if trust is absent/ambiguous, if the only route is `--trust`, or if owner authorization for the file write/restore is missing. Record only necessary trust status, not unrelated `~/.vibe` contents.
- [ ] **Step 2: Prove delivery before role work.** Use one no-tools invocation with the exact pinned Mistral model id. Require `reported_model == pinned`, telemetry off, no provider keys/endpoints, one admission/receipt and valid export/session identity. A missing model, fallback (including Mistral Medium 3.5), `model_unverified`, `model_mismatch`, paid-route indication or unexpected endpoint is a **full stop**, not a retry loop.
- [ ] **Steps 3–8: Execute in order and stop on a blocking failed prerequisite.** Each step first recomputes launch identity and native artifacts, then runs only its allocated calls. Stop that step and all dependent steps on strict-hook absence, artifact drift, trust loss, leaked sentinel, allowed out-of-scope action, missing usage receipt, stale identity, unbounded retry or model mismatch. A cell is observed only for the exact behavior actually proved; a failed optional behavior remains unknown/refused and cannot be inferred from another role.
- [ ] **Memory-specific stop conditions.** Stop D3/campaign on non-loopback URL, bearer in a file/log/usage row, token mode other than `0600`, memory call before service readiness, unauthorized memory operation, stale token/service, unreleased handle or surviving service process. Unaccepted `vibe_memory` never triggers acquisition.
- [ ] **Worker/campaign stop conditions.** Stop D4/campaign on any edit/write outside `work_dir`, enabled bash/process tool, missing strict denial, disabled reviewer edit/write not denied, forged/rebound resume acceptance, repeated drain or more receipts than physical invocations. Reviewer remains a campaign phase, not another stage.
- [ ] **Step 9: Restore and compare.** Stop remaining launches at budget exhaustion. Release managed services, confirm no gate process remains, compare before/after manifests with `cmp`, recheck disk/processes/ports, and restore the trust backup byte-identically if that route was authorized. Attribute any pre-existing service separately; never kill it. Publish only scrubbed evidence with no home paths, tokens, prompts containing private data or raw subscription exports.

**Cell evidence required before refused -> accepted:** a row is promoted only when its gate status is observed, `stale=false`, every prerequisite cell is observed with a repository-safe non-synthetic artifact, and the live launch identity equals the final candidate identity (`campaign_runner/executors/capabilities.py:73`). Otherwise it stays refused/pending with the failure named.

| Cell(s) | Minimum live evidence for that row |
|---|---|
| `identity`, `hook_policy_digest`, `discovery_dir` | Resolved executable version/hash, final normalized identity, run-owned native files and immutable project match on first launch and resume. |
| `argv`, `native_schemas`, `caps` | Actual scrubbed invocation/exports showing required flags, role tool lists, effective `min(role,governor)` cap and strict `pre_tool`; evaluator one turn/helper plain reply where applicable. |
| `trust` | Production nearest-ancestor persisted-trust pass without `--trust`; trust absent test refuses before spawn (using a separate synthetic path, not an unsafe live launch). |
| `model_delivery` | Export effective model exactly equals the pinned Mistral id; no fallback; mismatch fixture remains rejected offline. |
| `allowed_actions` | Real permitted project read and, worker only, real work-tree edit/write plus useful role output/artifact/receipt. |
| `denied_actions` | Actual hook/native denial records for secret, alias, recursive/outside operations, shell/process and delegation; reviewer/helper/evaluator edit/write denials; hook failure denies before execution. |
| `resume` | Same bound session/configuration resumes successfully; changed identity/session binding refuses; usage is a delta and no extra hidden invocation occurred. |
| `usage_semantics`, `quota_evidence` | Real nullable input/output/cache/cost counters and one ledger row per physical call; only genuine quota evidence if encountered—never manufacture or require a quota event to accept other cells. |
| `http_api_key_env`, `vibe_memory` | Authenticated loopback MCP readiness and allowed memory operations in the Vibe trace, bearer absent from artifacts/logs, correct project/tool scope, continuation and complete service cleanup. |

**Final acceptance:** the lead, not the gate runner, updates `capabilities-p3.json` and acceptance docs from scrubbed evidence. Each Vibe role is considered separately. Any missing prerequisite, changed identity, unspent-but-unproved cell or budget stop leaves that row refused; a successful campaign is not a substitute for an unproved cell. Report actual calls as `5 + calls_this_gate <= 30`, reserve usage and ancillary memory counts.

**Commands:** Task 6 has no offline command. The live runner records every exact invocation and exit code in its evidence report; model calls begin only after Step 1b passes.

**Identity impact:** only final live evidence may replace old refused/stale row identities with new accepted identities. Record every old -> new digest and bind it to the exact source revision and installed distribution.

## Lead answers (17 Nov 2026)

1. **Model id.** Not pinned offline. Task 6 Step 0 reads the model catalogue shipped in the installed Vibe package
   (source only, no launch) and pins the GLM model the owner asked for on 16 Nov if it is listed, otherwise
   `mistral-medium-3.5`. Step 2 is the proof; a fallback or a missing model is a full stop. The chosen id and its cost
   are recorded in the gate report.
2. **Trust.** The owner authorized (17 Nov, "Yes, with restore") the backed-up append of the gate project's resolved
   path to `trusted` in `~/.vibe/trusted_folders.toml`, with a byte-identical restore proved by `cmp`. Values in the
   file are never printed; nothing else under `~/.vibe/` is read or written. Valid for this plan's live gate only.
3. **Stub variable.** Move `VIBE_STUB_CONFIG` out of production: the tests configure the stub through an
   invocation-private wrapper or file, and the production allowlist carries no test-only name.
4. **Proxy variables (amends Task 2).** `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` and their lowercase forms are
   forwarded: they do not select a provider and members behind a proxy need them. Provider endpoint selectors
   (`*_BASE_URL`, `*_API_BASE`) and every `*_API_KEY` stay out.
5. **Known consequence of Task 2.** Vibe authenticates to the member's Mistral account from its own store
   (`~/.vibe/.env` or the keyring). A member who keeps `MISTRAL_API_KEY` only in the shell must run Vibe's own setup
   once; Magnolia's refusal or failure message for that case says so.

## Later only / not in this plan

- A Magnolia-rendered OpenRouter provider would need a separate approved design and credential-isolation protocol; Vibe's `ProviderConfig.api_key_env_var` resolves from the process environment, so it is not a safe add-on here.
- Upstream Vibe issues, additional providers/models and automatic capability promotion are out of scope.
