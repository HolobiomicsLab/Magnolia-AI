# Codex gate 4 evidence

This directory is the tracked, sanitised record of the live Codex worker/reviewer
gate run on 17 November 2026. The exercised source revision was
`abfcaf43e80a24c79c2ec1ee34459affe3b542d7`; the client was Codex CLI 0.169.0
(`739cda86e15d30a6e2b40cf8dcbbfb988aaf48b305b27065573bb8e9fd41d002`),
and the selected model was `gpt-5.6-luna`.

- `launch-identities.json` proves the client/model, persisted project-hook trust,
  argv shape, policy/configuration digests, effective caps, and the exact worker
  (`a5f58afa…`) and reviewer (`21acaebe…`) launch identities.
- `worker-policy.json` records permitted reads/searches, the confined worker edit,
  denied secret/alias reads, denied outside/control edits, denied shell and denied
  non-allowlisted memory use. Every routed inner call has a PreToolUse row.
- `reviewer-policy.json` records permitted reviewer reads and denied edit, shell,
  MCP-resource and delegation calls. The diagnostic run repaired the first run's
  wrong tool-name gap without repeating that run.
- `runtime-surface.json` records the code-mode host I/O probe, the worker/reviewer
  catalogs, the `write_stdin` exception, and the offline 0.169.0 switch inventory.

The lead accepted gate 4 as meeting D2: permitted reads were usable; secret and
alias reads were denied; shell was denied; worker edits were confined; reviewer
edits were denied; trust was normal and persisted; the code-mode host exposed no
direct file, network or process I/O; and every routed inner tool call was hooked.
The current branch at `167ad97cb990aa372c859a2e6f5a6de248aa047e` changes only
Task 11 daemon/admission files relative to the gated revision, so the rendered
Codex identities remain the gated identities and re-observation is not pending.

## Accepted limitations

The code-mode catalog is wider than each role's allowlist. The worker catalog also
contains non-allowlisted memory tools; both catalogs contain `exec_command`,
`write_stdin`, the three MCP-resource tools and five `multi_agent_v1__*` tools.
Enforcement therefore rests on the required hook's deny-unknown allowlist, not on
catalog removal. `write_stdin` is not hooked, but it cannot obtain a session because
the hooked `exec_command` call is denied. ChatGPT connected-app and web/image tools
were absent.

Offline `--help`, `exec --help`, and `features list` under a temporary HOME found
no documented per-tool catalog allow/deny switch. `--disable multi_agent` was
already present in the gated argv yet the v1 tools remained. Feature names such as
`shell_tool`, `unified_exec`, `unified_exec_tty`, and `write_stdin_approval` do not
document catalog-removal semantics, and no MCP-resource removal option is listed.
Changing those feature overrides would change argv and requires a new live gate.

The live gate did not spend another call on resume or quota exhaustion. Resume
remains identity-bound by the already tested executor contract; no account quota
signal was observed. Gate 4 did record one complete subscription ledger receipt per
physical invocation with native token/cache counters. No claim here promotes quota
evidence.

All absolute home paths, temporary gate/copy paths, process identifiers, session
identifiers, owner configuration values and tokens were removed. Tool names, hook
decisions, logical fixture targets, argv shape, versions, model and identities were
retained.
