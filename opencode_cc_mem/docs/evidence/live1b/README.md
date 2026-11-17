# Live pass 1b evidence (Claude roles on Claude Code 2.1.326)

Tracked, sanitised record of the 17 November 2026 live pass 1b on source revision
`03333f9ced9e117aeae36b5dcfbafefd6e95b38f`. Claude Code installed: 2.1.326
(`e214ad76dcc0e846b03be28ed1c2734aacd4e27acb527cb7f8a9e2bed644e837`); bound in
the committed table: 2.1.325 (`84590fef…`). Model `haiku` (resolved
`claude-haiku-5-5`), owner subscription.

The production capability table still refuses every Claude role
(`capability launch identity mismatch: client_version`). The runs here use a
CANDIDATE table, `capabilities-p3.candidate.json`: a copy of the snapshot table
in which only the four Claude rows' `cells.identity.value.{version,sha256}` and
`gate.value.client_identity.{version,sha256}` are set to the installed values
(16 changed leaves, nothing else).

- `launch-identities.json`: production-table and candidate-table dry results,
  then, per role, every normalized launch-identity component observed against
  the committed row. Only `client_version` and `client_sha256` differ for worker,
  reviewer, helper (native_read over docs/) and evaluator; `hook_source`
  `927d5975…` is equal. Also the prepared configuration digest, argv and child
  environment key names (no provider key, no `CLAUDE_CONFIG_DIR`).
- `worker-policy.json`: the worker's catalog, hook stream, every tool call with
  its hook decision, the hook transcript, the memory boot and the allowlisted
  `memory_get_context` call (hook pre and post rows), and the disk results.
- `reviewer-policy-R.json`, `reviewer-policy-R2.json`: two reviewer runs over a
  fixed synthetic worker output (the pass-1 Codex worker's `work/result.txt`).
  R's policy behaviour is correct, but haiku put strings in `followups`, so the
  production `validate_review` rejected it. R2 used a stricter format line and
  passed the validator. The input file is unchanged after both runs.
- `helper-policy.json`: the helper in its bound shape (`--tools Read`, reads
  limited to `docs/`). The no-tools helper shape is refused by the candidate
  table (`identity mismatch: settings`) and was not run.
- `evaluator.json`: one plain turn, no tools, `EVAL-OK 4`.
- `usage-ledger.json`: one subscription receipt per physical call (5).
- `capabilities-p3.candidate.json`: the candidate table that was used.

Removed from these files: home and temporary paths, user name, session and tool
call identifiers, invocation ids and the sentinel values.
