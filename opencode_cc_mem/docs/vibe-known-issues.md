# Mistral Vibe as a Magnolia client: known issues and accepted limitations

Applies to Mistral Vibe 3.1.2 (unified runtime), the version the four Vibe capability rows bind (worker,
reviewer, helper, evaluator; accepted 18 November 2026). Each entry says what happens, why, what Magnolia does
about it and what a member should do. Evidence: `docs/evidence/live-p4/` (`vibe9a/` for the `run_typescript`
probe, `vibe9b/` for the acceptance gate). Acceptance record: `docs/harness-acceptance-p4-vibe-2026-11.md`.

## Summary

Vibe is accepted with an **accepted limitation**, as the Codex worker and reviewer were: Vibe offers the model more
tools than a role allows, and cannot be told not to. Magnolia's strict `pre_tool` hook, which denies anything outside
the role's policy including unknown tools, is the enforcement boundary. Vibe's own deny-on-call is a second layer for
the shell and, in read-only roles, for writes and edits. Two of the offered tools, `run_typescript` and
`search_tool_functions`, run without any hook record. Every file, shell or memory call a `run_typescript` program
makes is hooked, and the program's sandbox has no filesystem, process or network access of its own.

## Accepted limitations (Vibe's design; reported upstream)

### 1. Every role is offered nine tools, including shell, write and edit

- **What happens.** In every call, every role is offered `todo`, `unified_harness_scratchpad`,
  `search_tool_functions`, `run_typescript`, `read_file`, `write_file`, `edit`, `bash` and `skill` (the worker
  also gets Magnolia's memory tools). That includes the reviewer, helper and evaluator, for which Magnolia disables
  `bash`, `edit` and `write_file`, and the worker, for which it disables `bash`.
- **Why.** Vibe's `enabled_tools` / `disabled_tools` settings and its `--disabled-tools` flag only make a tool
  "denied when called"; they do not remove it from what the model sees. Vibe's core enables its filesystem tools
  unconditionally. Upstream: [mistralai/mistral-vibe#1552](https://github.com/mistralai/mistral-vibe/issues/1552).
- **What Magnolia does.** The strict hook denies every call outside the role's policy before it runs: in the
  acceptance gate, every forbidden write, edit, shell command, secret read and alias read was denied with no effect,
  in every role. Magnolia also renders the names Vibe's filter matches, and switches off subagents and background
  processes (`enable_subagents = false`, `enable_background_processes = false`), which does remove those tools.
- **What a member sees.** The model may attempt a forbidden tool and receive "Magnolia required role policy denied
  this operation", then continue. Nothing changes on disk.

### 2. `run_typescript` and `search_tool_functions` leave no hook record

- **What happens.** Vibe's programmatic tool calling cannot be switched off in 3.1.2. Vibe does not call Magnolia's
  hook for the `run_typescript` call itself, nor for `search_tool_functions` (tool discovery). Upstream:
  [mistralai/mistral-vibe#1431](https://github.com/mistralai/mistral-vibe/issues/1431).
- **Why it is still safe, as far as observed.** Every tool call made from inside a `run_typescript` program reaches
  the hook like a direct call (probe 9a and gate 9b): nested reads and memory calls inside policy were allowed;
  nested outside writes, outside edits, secret reads and shell commands were denied with no effect. The program's
  sandbox has no `process`, `require`, `Deno` or `Buffer`; `import('node:fs')` fails; `fetch` exists but throws
  ("not supported in the sandbox"), and a loopback server received no request.
- **Residual risk.** The `run_typescript` boundary rests on Vibe's sandbox, which Magnolia cannot inspect: the Rust
  core is not shipped in the wheel. A Vibe update must be re-probed before its rows are rebound (see 8).
- **What a member sees.** Programs and tool searches do not appear in Magnolia's hook transcript. To audit them, read
  Vibe's own session journal for the run.

### 3. Denied attempts cost turns

- **What happens.** Because forbidden tools stay offered, a model sometimes spends turns on calls that are then
  denied. Reviewers run with a cap of 8 turns, helpers 40, workers 30 and evaluators 1. A one-turn evaluator that
  tries a tool ends with `turn_limit` and no answer.
- **What to do.** Keep reviewer and evaluator prompts short and say which tools are available. Read-only roles
  cannot search with `grep` (Vibe has no native grep tool and `bash` is denied); they read files instead.

### 4. `todo` and `unified_harness_scratchpad` are denied

Both are hooked, unknown to Magnolia's policy, and therefore denied. Neither touches the project (`todo` is an
in-session list; the scratchpad is a Vibe-private, per-session folder). The model works without them.

### 5. No record of which tools were offered

Vibe exports the effective configuration and the calls made, but no manifest of the tools offered per model request.
The live gates read the offered catalogue from the session journal under `--output-dir`
(`model_input.tool_catalog`), which is not a documented interface. The capability rows do not depend on it at run
time.

## Operating notes

### 6. Trust a project once, in Vibe

Vibe refuses a project it does not trust, and Magnolia never passes `--trust`. Open `vibe --workdir <project>` once
and accept Vibe's trust prompt. Magnolia refuses to launch Vibe in a project whose run files it cannot discover.

### 7. Credentials and notices

Vibe uses the member's own Mistral account; Magnolia never handles the key. On macOS the notice "Reading API keys from
the macOS Keychain" is expected; a member without Vibe credentials must run Vibe's own setup once. A member who
configured other models in Vibe sees "Settings for model … are ignored": harmless, because Magnolia pins the model
per run. The 3.1.2 catalogue offers `mistral-medium-3.5` only, and Magnolia refuses a run whose delivered model differs
from the pinned one.

### 8. A client update invalidates its accepted rows

Each accepted row binds the client's exact version and executable hash. When Vibe, Claude Code or Codex updates
itself, Magnolia refuses that client before launch ("capability launch identity mismatch") and a campaign stops in
`waiting_human`. The rows must be re-observed live on the new version, or the update rolled back. This happened
during the Vibe gate: Claude Code updated itself from 2.1.326 to 2.1.327.

### 9. Worker writes need the run's `work/` directory

The hook refuses a worker write or edit until `work/` exists (it fails closed). The campaign runner creates it;
hand-driven runs must create it too.

### 10. Cost, not quota

Vibe reports per-call cost and tokens, which Magnolia records in the usage ledger. It reports no quota or
rate-limit signal, so `quota_evidence` stays synthetic-only. The acceptance gates cost about US$1.40 of Mistral
usage in total.

## Fixed in Magnolia during Plan 4

- **`--output json` is one message array.** Vibe 3.1.2 prints one JSON array, not JSON lines. Before commit
  `43c574e` every Vibe answer reached Magnolia empty.
- **Tool-filter names.** Vibe's filter matches its Python tool names (`bash`, `write_file`, `edit`), not the core
  names (`file_system.bash`). Until commit `0f762c9` Magnolia rendered the core names, so Vibe's own deny-on-call
  never applied and only the hook protected. A member's `.vibe/config.toml` written by an older Magnolia may still
  list `file_system.bash`; it is harmless and is kept (the merge is append-only).
- **Identity tie.** When a run's `run_dir` equals its `project_dir`, the launch identity's path normalisation could
  differ between processes (`campaign_runner/launch.py`, `_identity_replacements`). The order is now total. Campaign
  layouts were never affected.

## Not covered live

- Vibe's schema rejects a `read_file` carrying both `path` and `file_path` before the hook runs, so the hook's
  "differing paths" rule has offline coverage only for Vibe.
- Models sent `path` rather than `file_path` for nested calls; the adapter accepted both.
- The full campaign step ran the Vibe worker in the real runner successfully, but the Claude reviewer half could
  not run (see 8).
