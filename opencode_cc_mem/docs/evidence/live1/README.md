# Live pass 1 evidence (Claude and Codex re-observation after fix B)

Tracked, sanitised record of the 17 November 2026 live pass on source revision
`03333f9ced9e117aeae36b5dcfbafefd6e95b38f` (Plan 3 head `912aec5` plus the
acceptance-doc re-sync). Codex CLI 0.169.0
(`739cda86e15d30a6e2b40cf8dcbbfb988aaf48b305b27065573bb8e9fd41d002`), model
`gpt-5.6-luna`. Claude Code installed on the machine: 2.1.326
(`e214ad76…`); bound in the table: 2.1.325 (`84590fef…`).

- `claude-production-refusal.json`: all four Claude roles are refused by the
  production capability table with `capability launch identity mismatch:
  client_version`. The client auto-updated to 2.1.326 at 04:18 CET the same day.
  Every other bound component matches, including `hook_source`
  `927d5975…`. No Claude model call was made.
- `launch-identities.json`: Codex worker and reviewer are accepted by the
  production table. All bound components are equal, and the configuration
  identities equal the rows: worker `ce93e584…`, reviewer `fd481207…`,
  `hook_source` `927d5975…`. The file also records the trust flow, the argv
  shape and one subscription ledger receipt per physical call. Codex
  helper/evaluator are refused.
- `worker-policy.json` and `reviewer-policy.json`: hook decisions for every
  routed inner call (worker 12/12, reviewer 6/6) and the disk results.
- `runtime-surface.json`: the code-mode catalogs. Fix B trimmed the worker
  memory catalog to the three allowlisted tools. The shell-session,
  MCP-resource and `multi_agent_v1__*` residual remains.
- `campaign-runner.json`: why the real campaign runner could not drive the
  harness-p3 protocols. The stage prompt is not copied by `init`, and
  `max_core_hours: 0` halts the run before any stage. Zero client launches.

Because of that, the Codex worker and reviewer ran through the production
`executor_for`/`prepare_launch` path, one role at a time, with `retries=0`. No
different-family campaign review, campaign receipt or continuity drain exists in
this pass. Removed from these files: home and temporary paths, process and
session identifiers, owner configuration values and sentinel contents.
