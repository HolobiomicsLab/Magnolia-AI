# Plan 3 Task 0 capability gate — offline record, 16 November 2026

Status: **all Claude Code, Codex, and Vibe roles are refused**. The owner has not authorised live Task 0 Steps 2–5. This record uses only the already committed P0 and round 6 evidence; waiting for a live run is not acceptance.

Machine-readable source: `campaign_runner/executors/capabilities-p3.json` (schema 1, generated from revision `b7504a580631d83ef424819e5659618dde87b1cc`). The loader rejects incomplete client identity, unknown statuses, absent observed artifacts, and synthetic artifacts claimed as observed.

## Initial gate

`identity` is observed for all rows from `docs/harness-probes-2026-11.md`. Vibe's recorded export establishes only the counter shape from one non-resumed free-model run; it does not establish parent/subagent inclusion, resume deltas, or paid cost semantics. Usage semantics therefore remain unknown for all three clients. Constructed quota controls are `synthetic-only` and do not certify an account signal.

| Client | Role | Gate | Blocking cells |
|---|---|---|---|
| Claude Code | worker | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Claude Code | reviewer | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Claude Code | service-less helper | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Claude Code | evaluator | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Codex | worker | **refused (D2)** | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Codex | reviewer | **refused (D2)** | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Codex | service-less helper | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Codex | evaluator | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Vibe | worker | **refused (D4)** | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Vibe | reviewer | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Vibe | service-less helper | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |
| Vibe | evaluator | refused | argv, model_delivery, discovery_dir, trust, hook_policy_digest, native_schemas, allowed_actions, denied_actions, resume, caps, usage_semantics |

D2 refuses both Codex campaign roles until the full normal-trust gate passes; a read-only sandbox or changed working directory is insufficient. D4 refuses Vibe worker edits until guarded edits and the complete path gate pass. P0 Codex and Vibe runs used `--dangerously-bypass-hook-trust` and `--trust`; they do not satisfy the normal persisted-trust cell.

## Evidence retained offline

- Executable/version/SHA-256 identities: `docs/harness-probes-2026-11.md#executables`.
- Small recorded derivatives: `tests/fixtures/executors/<client>/recorded/`; each names its committed P0 source.
- Vibe's recorded export preserves parent and subagent `input_tokens`, `output_tokens`, `cached_input_tokens`, and `total_tokens`.
- Parser-only quota controls: `tests/fixtures/executors/<client>/synthetic/quota.json`, each marked `"synthetic": true`.
- Round 6 context only: `docs/harness-acceptance-2026-11.md` and `docs/harness-campaign-2026-11-15.md`. These runs did not prove the complete role matrix.

## Task 6 offline enforcement evidence

Required mode is recognized from `MAGNOLIA_HOOK_POLICY_REQUIRED=1` before stdin or policy parsing. The real hook CLI now emits Claude/Codex `permissionDecision: deny` or Vibe `decision: deny` for malformed payloads, missing/invalid/schema-mismatched policy, unknown tools, and disallowed targets. A render exception, empty denial stdout, or failed denial write returns nonzero; this is not promoted to client acceptance because normal-trust fail-closed behavior remains unobserved.

Native target decoding remains `synthetic-only`: Claude `Read.file_path`, `Glob.{path,pattern}`, `Grep.{path,glob}`, `Edit.file_path`, and `Write.file_path` are in `tests/fixtures/executors/claude/synthetic/native-tools.json`; Codex only `Read.file_path` is in its corresponding fixture; Vibe `file_system.read_file.path`, `file_system.write_file.path`, and `file_system.edit.path` are in its corresponding fixture. Patch, notebook, move, unrecognized inputs, and every other uncited shape deny in required mode. The neutral policy tests cover multi-target patch/move semantics without claiming a client wire shape.

All client × role rows remain **refused**. The synthetic implementation evidence changes `native_schemas` from `unknown` to `synthetic-only`; it does not satisfy the observed prerequisite or release the live gate.

## Live work still owed

After the owner's explicit go, run the following ledger separately for each client. The hard cap is ten physical invocations per client across Task 0 and acceptance; every launch, resume, empty-output retry, and format repair consumes one. Stop rather than exceed the cap.

1. Normal persisted-trust worker launch: prove executable/argv, selected-model delivery, project discovery, immutable hook/policy digest, native schemas, allowed read, and one edit under its own `work/`.
   Claude gate: hooks delivered through `--settings` registered and ran.
2. Worker negative matrix: deny direct/aliased/recursive secret reads; traversal and symlink reads; new-target, multi-file, move, and outside-project/outside-extra-temp edits; native/MCP shell; member/unknown tools; delegation; and code-mode inner calls.
3. Reviewer launch: allow its named reads while denying every edit and every MCP/native action.
4. Service-less helper with no tools: prove its own model, cap, schemas, and denial contract.
5. Service-less helper optional-read variant: prove only the named read root and deny all other actions.
6. Evaluator: prove the independent one-turn, no-tools contract and selected-model delivery.
7. Same-session resume of invocation 1: prove identical role/config identity, tools, policy, and effective cap.
8. First half of a simultaneous worker/reviewer pair: prove role and model isolation.
9. Second half of that simultaneous pair: prove no cross-contamination and capture both outcomes.
10. Interactive rendering/session isolation: prove the selected model and member configuration do not leak into or weaken the noninteractive roles.

Use Claude `-p --output-format stream-json --verbose`, Codex `exec --json`, and Vibe `-p --output json --output-dir` with the implemented normal-trust configuration. Never use `--trust`, hook-trust bypass, permission bypass, alternate credential homes, or subscription credentials. Vibe keeps telemetry disabled. Capture a quota signal only if it appears naturally in these runs; do not exhaust an account.

Tasks 6–8 must bind the rows to the implemented policy, renderer, and executor digests, then repeat only changed observations inside the same cap before enabling a role. D3's project-bound HTTP memory service remains required for Vibe-memory acceptance; it does not block no-tools helper/evaluator proof.

The table's `discovery_dir` evidence is composed with runtime project discovery by Tasks 6–8; the static table value is never fed directly to launch.

## Task 8b offline gate — confined Codex reads

Task 8b adds a run-owned `magnolia-read` stdio server for Codex worker and reviewer launches. It does not change `agents/*.yaml` or `executors/capabilities-p3.json`: both Codex campaign rows therefore remain **refused (D2)** until new accepted capability evidence is recorded. Offline tests establish that the server and required hook independently apply the same requested/resolved path rules, recursive case-insensitive deny patterns and bounded descendant scan. The Codex adapter's `apply_patch` target decoding is synthetic-only until observed live.

The following Codex worker/reviewer checks are still owed; this task ran no client:

1. Through `magnolia-read`, allow named reads and reject traversal, case-variant denied paths, denied descendants and escaping symlinks. Exercise each denial once through the hook and once directly against the server to prove both layers.
2. Confirm the x10–x12 result still holds for the accepted launch identity: the outer code-mode `exec` has no file, process or network access, and every effectful inner `tools.*` call reaches `PreToolUse`.
3. Confirm direct `apply_patch` reaches `PreToolUse`, permits worker edits only under the run's canonical `work/`, and denies reviewer edits plus worker traversal, symlink, move and multi-target escapes.
4. Confirm `--disable apps` removes connected-app tools, user MCP servers/plugins are disabled, the selected model is explicit on launch and resume, and the supervising cap stops the process at its effective bound.

Only after those observations match the immutable policy/config identity may the capability projection mark a Codex worker or reviewer row accepted and release D2 for that role.
