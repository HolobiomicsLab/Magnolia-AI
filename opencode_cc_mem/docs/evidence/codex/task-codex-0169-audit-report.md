# Codex executor re-audit for codex-cli 0.169.0

> Sanitized tracked copy: home roots are `~`; ephemeral run/copy paths and owner-config values are omitted. Evidence claims and test results are retained.

Date: 2026-11-16. Branch/baseline: `feat/harness-executors-p3` at `cd200e4` or later. The audit used only the two named standalone binaries with a temporary `HOME` for `--version`, `--help`, `exec --help`, `features list`, and config-load errors. It did not run `codex exec`, contact a model or network service, read `~/.codex/auth.json` or the keychain, or read/write the owner's `~/.codex/config.toml`.

## Result

The executor now targets the installed `codex-cli 0.169.0`, remains fail-closed, and keeps all four Codex rows refused pending a live gate. Worker/reviewer argv disables every new 0.169 feature that can expose a tool, network/provider, browser, image/media, agent/sub-agent, goal, plugin/app, memory/context, or code-execution path. The final-argv guard additionally refuses the new `--cyber-access-program` flag; the new `--trust` flag was already refused. `gcp` is now treated as a built-in provider. The two fix-round-3 minors are closed: glued-only reasoning-effort conflicts are rejected, and explicit child-environment keys come from the dictionary that supplies their values.

## Offline client diff (quoted)

Identity:

```text
- codex-cli 0.154.0; realpath .../0.154.0-aarch64-apple-darwin/bin/codex; sha256 4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc
+ codex-cli 0.169.0; realpath .../0.169.0-aarch64-apple-darwin/bin/codex; sha256 739cda86e15d30a6e2b40cf8dcbbfb988aaf48b305b27065573bb8e9fd41d002
```

`--help` / `exec --help`:

```diff
-  agents            Browse all agent sessions on the shared local app-server daemon
+  agents            Open the agent command center to browse and switch sessions
+  migrate-nfs-db    [experimental] Migrate configured SQLite databases to DELETE mode in place
+      --no-daemon
+          Run without the shared background server, even if it is already running
+      --cyber-access-program <PROGRAM>
+          Request an experimental Cyber access program for this turn
+          [possible values: standard, daybreak_blue, daybreak_red]
+      --trust
+          Save trust for this project and future chats, including MCP commands outside the sandbox.
```

`features list`: no feature name disappeared. The complete added-name and changed-state diff was:

```diff
+ agent_message_board under-development false
+ agent_message_board_mcp under-development false
+ analytics_plan_history experimental false
+ api_key_cyber_access_programs stable true
+ api_key_model_discovery stable true
+ app_server_websocket_compression under-development false
+ apps_early_resource_discovery under-development false
+ async_compaction under-development false
+ async_compaction_root_only under-development false
+ browser_annotation_api stable true
+ cli_daybreak stable true
+ cli_resume_account_check under-development false
+ code_mode_host_grpc stable true
+ code_mode_host_output_truncation stable true
+ code_mode_only_strict_3p_tools under-development false
+ code_mode_tool_description_first removed false
+ code_mode_tool_search under-development false
+ codex_apps_mcp_2026_07_28 under-development false
+ command_sessions_world_state under-development false
+ credential_masking under-development false
+ critical_path_timing under-development false
+ daemon_auto_start stable true
+ daybreak_program_propagation under-development false
+ defer_mailbox_preemption under-development false
+ executor_capability_discovery_v2 under-development false
+ gateway_setup_wizard stable false
+ gcp_setup_wizard stable false
+ guardian_conversation_history_tools under-development false
+ guardian_denial_reconsideration under-development false
+ guardian_include_additional_context stable true
+ guardian_native_multi_agent_history under-development false
+ guardian_recorded_items under-development false
+ guardian_root_handoff_context under-development false
+ guardian_trust_orchestrator_connectors under-development false
+ guardianv2_decisions_comparison under-development false
+ guardianv3 under-development false
+ hosted_search_policy under-development false
+ image_generation_animation under-development false
+ in_app_voice stable true
+ incremental_exec_tools under-development false
+ incremental_tools stable true
+ incremental_tools_legacy stable false
+ incremental_tools_legacy_gcp stable true
+ instant_interrupt under-development true
+ interrupt_on_cancel under-development false
+ login_shell_package_path experimental false
+ mcp_connection_sharing under-development false
+ mcp_lazy_stdio_startup under-development false
+ mcp_tool_list_changed under-development false
+ model_catalog_in_context under-development false
+ multi_agent_v2_dynamic_tools under-development false
+ multi_env_copy_file under-development false
+ nonfatal_clock_read_errors under-development false
+ omit_skill_descriptions under-development false
+ output_token_replay under-development false
+ plugin_discovery_prefetch removed false
+ prediction_preserve_parent_tools stable true
+ prefer_mxc under-development false
+ retain_client_tool_outputs stable true
+ runtime_agent_instances under-development false
+ send_message_to_user_async under-development false
+ simplified_selection_hints under-development false
+ skill_catalog_deltas under-development false
+ stable_environment_tools removed false
+ subagent_default_context_limits under-development false
+ subagent_thread_environments under-development false
+ suppress_model_change_announcements stable false
+ system_proxy_fallback stable true
+ tui_extensions_statusline under-development false
+ tui_multi_backend under-development false
+ tui_workspace under-development false
+ ultrafast_mode stable true
+ untrusted_as_tool_output stable true
+ use_xaa under-development false
+ wake_on_agent_messages under-development false
  apply_patch_preserve_line_endings: under-development false -> removed false
  bedrock_setup_wizard: under-development false -> stable true
  code_mode_interrupt: under-development false -> stable true
  deferred_tool_world_state: under-development false -> stable true
  guardian_ext: under-development false -> removed false
  guardian_reuse_parent_compaction: under-development false -> stable true
  guardianv2.thread_context: under-development false -> removed false
  mcp_oauth_refresh_coordination: under-development false -> stable true
  multi_agent: stable true -> deprecated true
  personality: stable true -> removed false
  realtime_conversation: removed false -> removed true
  reasoning_effort_override: under-development false -> stable true
  remote_compaction_v2: stable true -> removed false
  respect_system_proxy: under-development false -> experimental false
  shell_zsh_fork: under-development false -> removed false
  transcript_v2: under-development false -> removed false
  unified_exec_zsh_fork: removed true -> removed false
  unified_image_budget: under-development false -> stable true
  worktrees: experimental false -> removed true
  write_stdin_approval: under-development false -> stable true
```

The disabled set is the source-of-truth `RESTRICTED_FEATURES` tuple. It includes the added/currently-on API-key discovery, browser annotation, daemon, voice, incremental/GCP tools, parent-tool retention, tool-output retention, system-proxy fallback and untrusted-tool-output paths, plus the new inactive agent/MCP/app/plugin/search/image/memory/code-execution variants. It also pins the security-relevant state changes for Bedrock setup, guardian parent context, MCP OAuth, reasoning override, image budgeting and write-stdin approval. The removed, immutable `worktrees` state is documented below instead of being passed as an ineffective disable switch.

New rows deliberately not disabled, each because it does not open a requested surface:

- `app_server_websocket_compression`: transport compression only.
- `cli_resume_account_check`: account validation on resume, not a capability.
- `code_mode_host_output_truncation`: output-size handling under the separately disabled host.
- `code_mode_tool_description_first`: removed and false.
- `credential_masking`: defensive redaction.
- `critical_path_timing`: timing telemetry.
- `defer_mailbox_preemption`: scheduler ordering only.
- `instant_interrupt`: cancellation timing only.
- `interrupt_on_cancel`: cancellation propagation only.
- `nonfatal_clock_read_errors`: clock-error handling only.
- `omit_skill_descriptions`: prompt presentation only; skill discovery itself remains closed.
- `plugin_discovery_prefetch`: removed and false; plugin/app surfaces remain closed separately.
- `realtime_conversation`: removed and immutable true; `--disable` cannot change it, so it is not a feature-selectable surface.
- `simplified_selection_hints`: selection UI hints only.
- `stable_environment_tools`: removed and false.
- `suppress_model_change_announcements`: cosmetic announcement control.
- `tui_extensions_statusline`: TUI display only.
- `tui_multi_backend`: TUI selection plumbing only.
- `tui_workspace`: TUI workspace presentation only.
- `ultrafast_mode`: model-speed mode, not a tool/network/approval surface.
- `worktrees`: removed and immutable true; `--disable` cannot change it, so it is not a feature-selectable surface.

## Configuration evidence

Every production pin loaded under 0.169.0 with exit 0: `web_search="disabled"`, both `sandbox_workspace_write.*` pins, the complete `shell_environment_policy` table, `experimental_use_profile=false`, and `notify=[]`. A configured MCP plus `mcp_servers.demo.enabled=false` loaded; `plugins.demo.enabled=false` loaded. `mcp_servers.demo.tools.read_file.approval_mode="approve"` loaded, while `"invalid"` failed with `unknown variant ... expected one of auto, prompt, writes, approve`.

The offline provider matrix accepted exactly `openai`, `ollama`, `lmstudio`, `amazon-bedrock`, `amazon-bedrock-runtime`, and `gcp` among the probed candidates; `azure`, `vertex`, `openrouter`, `oss`, `responses`, `bedrock`, and `gateway` were refused as not found. Exact-key reading still holds: malformed exact `web_search`, `sandbox_workspace_write.network_access`, `shell_environment_policy.inherit`, `model_provider`, and `notify` values failed, while uppercase variants of each loaded and were ignored. Magnolia continues to refuse ambiguous case/hyphen variants rather than rely on that ignore behavior.

## Capability rows: old -> new

All four rows stay `refused`; no capability cell was promoted. The configuration identities were recomputed and remain unchanged because the rendered role files did not change: worker `6a5923fb...19e`, reviewer `cca3c044...5e61`, helper `c38546a4...fe3`, evaluator `aa29a9f1...64fe3`. For each row, client identity changes from `0.154.0 / 4f859826...95afcc / ...0.154.0.../codex` to `0.169.0 / 739cda86...d002 / ...0.169.0.../codex` in both the refused gate and identity cell.

## Test-state isolation and owner files

Both campaign and memory test trees now set temporary `HOME`, `XDG_STATE_HOME`, and `XDG_CONFIG_HOME` before test modules import. Their session-wide autouse guards snapshot only path listings and nanosecond mtimes under the real `~/.local/state/magnolia`, `~/.config/magnolia`, and `~/.magnolia`, then fail at teardown on any create/delete/mtime change. The guards never read owner file contents.

Owner-state metadata was checked before and after the tests without reading file contents. Specific owner-state paths, names, and timestamps are omitted from this tracked copy.

## Verification

- Offline 0.154.0/0.169.0 identity/help/features/config probes: passed; no model execution.
- Focused executor/launch/capability/isolation selection on the final source: `310 passed in 33.24s`.
- Memory `tests/` on source: `1995 passed, 16 skipped in 51.10s`; on an independent rsync copy `[temporary copy omitted]`: `1995 passed, 16 skipped in 68.22s`.
- Campaign `tests/` on source: `938 passed, 1 skipped, 14 failed in 96.93s`; on the final rsync copy: `938 passed, 1 skipped, 14 failed in 99.26s`. Both runs emitted the complete pytest summary and the execution wrapper then reported exit 137. The same 14 failures are unrelated pre-existing/environment cases: five daemon default/discovery expectations, two process-group signal operations denied by the sandbox, five loopback/port tests denied by the sandbox, and two phase-1 baseline assumptions (`post_run_assess` absent and no `python` executable). Every modified executor, launch, capability and isolation test passed in both trees.
- All suites used an isolated basetemp `[temporary path omitted]`. The post-run metadata-only owner-state listing is unchanged from the pre-run listing above. `git diff --check`, capability JSON parsing and Python compilation pass.

## TM-4 remediation addendum (2026-11-17)

Mission A closed the read-server and guard gaps without changing the Codex client identity or `hook_source`. Offline `features list` on 0.169.0 confirmed `remote_plugin`, `skill_mcp_dependency_install`, `view_image`, `untrusted_as_tool_output`, and `write_stdin_approval` are accepted stable feature names. Worker/reviewer now disable the first three and explicitly enable the two protective defaults. Round 1 covered only the separated `-a VALUE` approval override; Round 2 covers all five supported spellings: `-a VALUE`, `-aVALUE`, `-a=VALUE`, `--ask-for-approval VALUE`, and `--ask-for-approval=VALUE`. Final argv validation also covers glued model/sandbox forms, unpinned `sandbox_mode`, `--search`, `--remote`, `--ignore-rules`, `--trust`, and conflicting feature overrides. Conditional child-owned environment names now come from the same dictionary that supplies their values.

The explicit `MAGNOLIA_PROJECT_DIR` read-server binding changed only the worker/reviewer native-file and configuration identities. Worker changed `6a5923fbb04fbefcf24b7debfad2f9f6b6cd351655e3393c967c12fe16eef19e` -> `a5f58afaa3aa6fa7efd4949f048fa8cbbba155a0a68d035b8424d2e57d50fadb` (`native_files` `eae00d1cd21b37621197eb744847ea32e1279db4ebdd8617f044e332ef4ec7e2` -> `c9029176512f058f08bab775d765e9bd85b0c589e62890fbc6a03ed77b16fd12`). Reviewer changed `cca3c04408e103ec008bd07315f09c0240a736a69ee120ce68f95b19eec3653e` -> `21acaebecfb171ce22b54550abcdceffa4b9ae359cbf7d306c5587847762485f` (`native_files` `73c726565df452cff38dbd97d9c331d850a3b9dc7770befe1f3446fb99cbf041` -> `fe097f4adff24720c34b646d31add9d58b95116daac95bcf407c4ddba57b2d9d`). Helper/evaluator and `hook_source=914889cbbedbefc8729e56146f6793272c03c9bef9cc61775513ca7d4c16ab89` are unchanged. All four rows remain refused.

The final source and independent-copy results are identical: memory `2002 passed, 16 skipped`; campaign `949 passed, 2 skipped, 14 failed`, limited to the established five daemon expectations, two sandbox-denied process-group cases, five sandbox/loopback service cases, and two phase-1 assumptions. The focused modified campaign files passed `321 passed, 1 skipped` and the focused read-server/isolation selection passed `20 passed`.

## Gate 3 finding: code mode is required (2026-11-17)

The live gate at snapshot `6e4a157` failed for worker and reviewer because their
argv disabled `code_mode_host`. The 0.169.0 catalog reports every available model
as `tool_mode: "code_mode_only"`; with the host disabled, the client said
`Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed`
and the allowed MCP reads disappeared with every other inner tool. The outer
`exec` failed in the client router, so no PreToolUse event reached Magnolia. This
is a functional failure, not evidence that the policy denied the attempted work.

Worker and reviewer now explicitly enable both `code_mode_host` and
`code_mode_host_grpc`. The latter is retained because the installed 0.169.0
package ships the host and separate gRPC/legacy remote-session transports, while
the offline feature inventory reports both host flags as stable and on. Disabling
`code_mode_host_grpc` offline leaves `code_mode_host=true`, which proves the flags
are independently configurable but does not prove that the next live 0.169.0
session will select a working fallback. Enabling the installed default is therefore
the fail-closed choice for the next gate.

The surface-widening variants remain disabled. Offline 0.169.0 accepts each
disable independently while leaving the host enabled:

```text
code_mode_only_strict_3p_tools  under development  false
code_mode_tool_search           under development  false
incremental_exec_tools          under development  false
incremental_tools               stable             false
incremental_tools_legacy        stable             false
incremental_tools_legacy_gcp    stable             false
```

The 0.169.0 host-disabled diagnostic names only `code_mode_host` (plus the host
installation) as the requirement for `exec`; the optional flags above are
independently accepted delivery/search variants, not host prerequisites. They
therefore stay off pending the live gate. The earlier x10–x12 runs separately
found that an enabled JavaScript host had no file-system, process or network API
of its own. Every external effect went through an inner `tools.*` call; x5
established that those inner calls reach PreToolUse. `code_mode_host_output_truncation`
stays at the client's stable default because it changes output handling rather
than tool reachability.

The final-argv validator is now role-aware. A later `--disable code_mode_host`
or equivalent feature override is refused for worker/reviewer, while the existing
helper/evaluator host disable and all prior bypass checks remain intact.

Configuration identities recomputed from the modified source are unchanged,
because the role's rendered files and policy bytes did not change; argv is a
separate live-gate cell:

- worker: `a5f58afaa3aa6fa7efd4949f048fa8cbbba155a0a68d035b8424d2e57d50fadb` -> same;
- reviewer: `21acaebecfb171ce22b54550abcdceffa4b9ae359cbf7d306c5587847762485f` -> same.

All Codex capability rows remain `refused`. The next gate must re-verify on
0.169.0 that the enabled host starts, allowed inner reads/worker edits succeed,
forbidden effects are denied by the production hook, and the runtime still has no
independent file-system, process or network path.

Verification on the final source: the Codex executor file passed `142` tests;
executor/launch/capability selection passed `332` with `1` skip. Memory passed
`2022` with `16` skips both in place and from an independent rsync copy. Campaign
tests produced the same result in both locations: `980 passed, 2 skipped, 14 failed`,
limited to the established five daemon expectations, two sandbox-denied process-group
cases, five sandbox/loopback service cases and two phase-1 assumptions. No modified
Codex test failed.


