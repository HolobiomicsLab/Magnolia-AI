# Codex live-gate fixes before XW/XR rerun

> Sanitized tracked copy: home roots are `~`; ephemeral run/copy paths and owner-config values are omitted. Evidence claims and test results are retained.

Date: 2026-11-16. Branch/starting revision: `feat/harness-executors-p3` at `e51696604bff` or later. This work used only offline Codex introspection; it did not run `codex exec`, contact a model, read `~/.codex/auth.json`, inspect keychain entries, or write the owner's `~/.codex/config.toml`. The approval-shape probe used a temporary `HOME`.

## Result

The XW/XR blockers found in the gate on `f5a5532` are fixed in the rendered launch contract, with repository tests written RED first. Codex rows remain refused and no Codex capability cell was promoted to observed; the lead must rerun XW and XR live.

1. **Exact per-tool approval.** Codex 0.154.0 accepted the offline override `mcp_servers.demo.tools.read_file.approval_mode="approve"`. Replacing `approve` with `invalid` failed configuration loading with: `unknown variant 'invalid', expected one of 'auto', 'prompt', 'writes', 'approve' in mcp_servers.demo.tools.read_file.approval_mode`. The rendered TOML therefore uses the exact supported shape:

   ```toml
   [mcp_servers.magnolia-read.tools.read_file]
   approval_mode = "approve"
   ```

   The renderer derives these tables from the role MCP allowlist: worker gets the three `magnolia-read` tools plus the three `compchem-memory` tools; reviewer gets only the three read tools; helper/evaluator get no governed MCP server. Native-artifact validation refuses missing, extra, or differently configured approvals. Unknown tools remain unapproved under `approval_policy="never"`.

2. **No source-tree bytecode.** Both Codex stdio server definitions name `PYTHONPYCACHEPREFIX` in `env_vars`; `_effective_environment` supplies a fresh path below the invocation directory. The isolated `magnolia-read` command remains `python -I <repository runner.py> --read-server`. An integration test copies the memory package to a second absolute, read-only source path, calls `tools/list` on both `--memory-server` and `--read-server`, and verifies that no `.pyc` or `__pycache__` appears below that copy while bytecode is written below the run cache.

3. **Codex launch identity.** The resolved Codex binary is versioned with the permitted `codex --version` call (`codex-cli 0.154.0`), SHA-256 bound by realpath, and re-hashed immediately before spawn. Configuration identity now covers only normalized run-owned `settings.json`, `mcp.json`, `.codex/config.toml`, `.codex/hooks.json`, and `policy.json`; owner configuration is excluded. A two-checkout test proves both the configuration digest and all normalized launch components are path-independent.

4. **Optional tool surfaces closed.** Offline `codex features list` reported `browser_use`, `browser_use_external`, `browser_use_full_cdp_access`, `code_mode_host`, `goals`, `image_generation`, `in_app_browser`, and `multi_agent` as enabled features, with `standalone_web_search` present but false. Supplying the production `--disable` flags reported every one false. Worker/reviewer argv disables all of them as well as `apps`; helper/evaluator retain the code-mode-host and apps closure. Named policy tests additionally prove denial of `exec`, `web__run`, image-generation, multi-agent, and goal calls when absent from the role allowlist. The prior x9 evidence already established that `--disable code_mode_host` takes effect; the live rerun must confirm the combined argv surface.

5. **Owner skills excluded.** Offline feature evidence showed `skip_host_skill_discovery` exists; `--enable skip_host_skill_discovery` changed it from false to true. Worker/reviewer argv now supplies this switch without changing `CODEX_HOME`.

No forbidden trust/approval bypass flag is rendered. `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and `VIBE_HOME` remain forbidden, and secret values remain launch-environment-only.

## Capability identity: old → new

Previously every Codex gate had no `configuration_binding`, so projection produced no normalized `launch_identity`; the configuration digest also included the owner's complete Codex file and was path-dependent. The refused rows now carry these recomputed bindings while retaining all existing cell statuses and refusal state:

| Role | Old binding | New run-owned configuration SHA-256 |
|---|---|---|
| worker | absent / owner-bound | `6a5923fbb04fbefcf24b7debfad2f9f6b6cd351655e3393c967c12fe16eef19e` |
| reviewer | absent / owner-bound | `cca3c04408e103ec008bd07315f09c0240a736a69ee120ce68f95b19eec3653e` |
| helper | absent / owner-bound | `c38546a4b203f21061cabb3614d585fcc47af6c221741818e122fc689a055e61` |
| evaluator | absent / owner-bound | `aa29a9f118f6152b6278a5bb14d7974ec0778c5027e40fceb08dc5aa88264fe3` |

The bound client remains version `0.154.0`, SHA-256 `4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc`. Full per-role normalized components are recorded in `opencode_cc_mem/campaign_runner/executors/capabilities-p3.json`.

## Verification

- RED before implementation: `5 failed, 11 passed` (approval/cache/identity focused contract selection).
- Final focused executor, launch identity, capability-table, required-policy, and read-server integration selection: `257 passed, 2 skipped`.
- Memory covering suite from the source checkout: `1992 passed, 16 skipped`; from an independent rsync copy `[temporary copy omitted]`: `1992 passed, 16 skipped`.
- Campaign covering suite from the source checkout: `850 passed, 1 skipped, 14 failed`; the final rsync copy produced the identical result. Seven failures are sandbox limitations allowed by the mission (two process-group signal tests and five loopback/port tests, all `Operation not permitted`). The remaining seven are unrelated failures already reproduced from an untouched `git archive HEAD`: five legacy `agents_daemon` discovery/default-lane expectations and two legacy phase-1 expectations (`post_run_assess` and a `python` executable on `PATH`). No Codex-livefix test failed.
- Every pytest command used an isolated basetemp `[temporary path omitted]`; the copy suites ran with `PYTHONPATH` rooted only in the copied checkout.

## Remaining transition

XW and XR live reruns are pending. Receipt of this report is not acceptance: keep every Codex role refused and do not mark any Codex cell observed until those gates are reviewed and accepted.

## Fix round 1

Date: 2026-11-16. Starting revision: `bf9dcdcc281203748968329cfe76f55ebfe4a820`. No Git state change, client model run, network call, paid call, auth/keychain read, or owner-config write was made.

### Result

The reviewer’s I1/I2 and M1–M4 rulings are implemented with repository tests written RED first (`31 failed, 219 passed`) and then green (`249 passed`). Worker and reviewer now pin `web_search="disabled"`, `sandbox_workspace_write.network_access=false`, `sandbox_workspace_write.writable_roots=[]`, and `shell_environment_policy={inherit="none",ignore_default_excludes=false,exclude=[],set={},include_only=[]}`; they retain the `standalone_web_search` disable and name-based policy denial and also pass `--disable computer_use`.

Offline Codex 0.154.0 probes used only `--version`, help, `features list`, and configuration loading under temporary `HOME`. Each selected value loaded with exit 0. The rejected controls established the schemas exactly: web search reported `expected one of disabled, cached, indexed, live`; network access reported `expected a boolean`; writable roots reported `expected a sequence`; shell inheritance reported `expected one of core, all, none`. `model_provider="openai"` loaded with exit 0. `features list` reported `computer_use stable true`, and the production disable turns it off.

Before each spawn, the executor now rereads the owner TOML and names/refuses: a non-`openai` `model_provider`, any `model_providers` table, top-level or profile base-URL equivalents, and profiles containing those redirects. Every pinned setting and every refused redirect has a test. The digest of owner settings that still influence execution is recorded in the session binding only; it is excluded from the path-independent launch identity. User MCP/plugin names are still disabled dynamically, and persisted hook trust remains available because `--ignore-user-config` is not used.

The final argv is checked immediately before `run_process`. The guard now covers `--dangerously-bypass-approvals-and-sandbox`, every forbidden `flag=...` form, unsafe `approval_policy`/`sandbox_mode` and `dangerously_*` `-c` overrides, and danger-full-access sandbox flags. Fresh prompts are preceded by `--`; resume thread names are restricted to a bounded safe format and the prompt is separately preceded by `--`. The Codex `--version` subprocess receives the launch `PATH` and a disposable `HOME` under the run’s executor directory.

### Capability binding: old → new

The rendered configuration SHA-256 values recompute unchanged; the normalized binding now also names `model: gpt-5.6-luna`, so a different model is refused as a `model` identity mismatch:

| Role | Old | New |
|---|---|---|
| worker | `6a5923fbb04fbefcf24b7debfad2f9f6b6cd351655e3393c967c12fe16eef19e`, unnamed model | same SHA-256 + `model=gpt-5.6-luna` |
| reviewer | `cca3c04408e103ec008bd07315f09c0240a736a69ee120ce68f95b19eec3653e`, unnamed model | same SHA-256 + `model=gpt-5.6-luna` |
| helper | `c38546a4b203f21061cabb3614d585fcc47af6c221741818e122fc689a055e61`, unnamed model | same SHA-256 + `model=gpt-5.6-luna` |
| evaluator | `aa29a9f118f6152b6278a5bb14d7974ec0778c5027e40fceb08dc5aa88264fe3`, unnamed model | same SHA-256 + `model=gpt-5.6-luna` |

The reviewer P1 probe rerun against the modified rsync tree reported `component diffs: none`, `config identity match: True`, and `prepared config_digest match: True` for all four roles. Codex rows remain refused; no Codex cell was promoted to observed.

### Verification

- Focused executor, launch, and capability tests: `249 passed`.
- Memory covering suite from source and an independent rsync copy `[temporary copy omitted]`: `1993 passed, 16 skipped` in each.
- Campaign covering suite from source and the same copy: `877 passed, 1 skipped, 14 failed` in each. The failures are the unchanged unrelated baseline: five legacy `agents_daemon` defaults/discovery expectations, two process-group signal denials and five loopback/port denials from the sandbox, plus two legacy phase-1 expectations (`post_run_assess` and a `python` executable on `PATH`). No Codex live-fix test failed.
- Every pytest invocation used an isolated basetemp `[temporary path omitted]`; copy suites imported only the copied checkout.

XW and XR live reruns remain pending. Receipt is not acceptance: all Codex roles stay refused and no Codex capability cell becomes observed until those dependent gates are reviewed and accepted.

## Fix round 2

Date: 2026-11-16. Starting revision: `b7dc87ac1121f536d8b1731330f2f449b034a6b7`. No Git state change, client model run, network or paid call, auth/keychain read, owner-config write, or agent spawn was made. Codex ran only for offline `--version`, `--help`, `features list`, and configuration-load checks under a temporary `HOME`.

### Result

The five lead rulings are implemented with repository tests written RED first (`38 failed, 61 passed`) and then green (`100 passed` for the Codex executor file; `287 passed` for executor, launch, and capability projection together).

1. Worker and reviewer add the independently load-checked `shell_environment_policy.experimental_use_profile=false` pin. A non-empty owner `shell_environment_policy.filters` is refused by its full key. Owner `set` names are checked case-insensitively against every ruled protected class and the actual launch-environment names. The benign `NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S` is allowed; the digest projection keeps its name and SHA-256 of the value, never the value.
2. Offline Codex 0.154.0 configuration loading established built-in provider ids `openai`, `ollama`, and `lmstudio`: all three loaded with exit 0; `azure` and `openrouter` failed with `Model provider <id> not found`. `--help` independently names `lmstudio` and `ollama` as local providers. Owner definitions of any of the three built-ins are refused by `model_providers.<id>`. An unselected custom table such as `openrouter` is allowed and contributes only its name to the owner digest. Non-`openai` selection, base-URL, and profile refusals remain.
3. `owner_config_digest` remains a mandatory valid SHA-256 in every new stored binding, but its value is removed from binding equality. A changed benign owner value resumes and writes the new digest; a bf9dcdc-shaped row without the field is refused. `model` and the configuration identity remain compared, and a model change is refused. Non-executing `projects`, `hooks.state`, `plugins`, `marketplaces`, `desktop`, `tui`, `notice`, and force-disabled `mcp_servers` are excluded.
4. Digest filtering is role-aware. Worker/reviewer omit only their actual isolation pins and restricted feature values; helper/evaluator retain `web_search`, all `features`, `shell_environment_policy`, and `sandbox_workspace_write` in the digest. Their argv was not given new pins.
5. The final-argv guard now refuses `--yolo`; separate, glued, and `=` profile forms; separate and `=` `--add-dir`; glued and `=` short sandbox bypasses; mismatching pinned `-c`/`--config` values; and both forms of `--enable` for restricted features. Each spelling has a spawn-level no-client-start test.

### Capability binding: old → new

Recomputation produced no normalized component change: this round changes runtime argv validation and the audit-only owner digest, not the run-owned configuration inputs. The four refused Codex rows therefore retain their current model-bound identities:

| Role | Old | New |
|---|---|---|
| worker | `6a5923fbb04fbefcf24b7debfad2f9f6b6cd351655e3393c967c12fe16eef19e` + `model=gpt-5.6-luna` | unchanged |
| reviewer | `cca3c04408e103ec008bd07315f09c0240a736a69ee120ce68f95b19eec3653e` + `model=gpt-5.6-luna` | unchanged |
| helper | `c38546a4b203f21061cabb3614d585fcc47af6c221741818e122fc689a055e61` + `model=gpt-5.6-luna` | unchanged |
| evaluator | `aa29a9f118f6152b6278a5bb14d7974ec0778c5027e40fceb08dc5aa88264fe3` + `model=gpt-5.6-luna` | unchanged |

`capabilities-p3.json` was not changed: every Codex gate remains refused and no cell was promoted to observed.

### Verification

- Focused executor, launch, and capability tests: `287 passed`. Syntax/undefined-name lint (`ruff --select F,E9`) passed. `git diff --check` passed.
- Memory covering suite, source and an independent rsync copy `[temporary copy omitted]`: identical `1993 passed, 16 skipped, 1 failed`. The sole failure is the pre-existing OpenRouter project/role session-id test; neither its source nor its test differs from starting HEAD.
- Campaign covering suite, source and the same copy: identical `915 passed, 1 skipped, 14 failed`. These are the unchanged baseline classes: five legacy daemon discovery/default-lane expectations, two sandbox-denied process-group signal tests, five sandbox-denied loopback/port tests, and two legacy phase-1 expectations (`post_run_assess` and a `python` executable on `PATH`). No Codex live-fix test failed.
- Every pytest invocation used an isolated basetemp `[temporary path omitted]`; copied-suite imports were rooted only in the copied checkout, whose two modified-file SHA-256 values matched the source checkout.

XW and XR live reruns remain pending. Receipt is not acceptance: all Codex roles stay refused and no Codex capability cell becomes observed until those dependent gates are reviewed and accepted.

## Fix round 3 (recorded by the lead; the worker was interrupted)

The Codex worker (run directory omitted) exited 1 after 23 minutes when authentication
was revoked (401 `token_revoked`) during its final copy re-run, before writing this section. From
its last progress messages (events.jsonl items 52-63): offline 0.154.0 provider-load matrix `openai`, `ollama`,
`lmstudio`, `amazon-bedrock`, `amazon-bedrock-runtime` load; `openrouter`, `azure`, `responses`, `lm-studio` do not.
Focused executor/launch/capability tests 301 passed; source memory suite 1994 passed / 16 skipped; source campaign
suite at the known sandbox baseline; source and first copy matched. Last change before the interruption: optional
Vibe/model variables count as contract-owned only when the route sets them. Ruff not installed; `git diff --check` clean.

Lead verification outside the sandbox from an independent copy `[temporary copy omitted]`: memory 2006 passed /
4 skipped / 0 failed; campaign 935 passed / 9 known baseline failures. Files: `codex.py`, `launch.py`,
`tests/test_executor_codex.py`. RED-first evidence was not reported by the worker; the reviewer checks the new tests
against 403a39d.

