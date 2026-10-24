# Campaign supervisor: third assessment

Date: 2026-10-24
Target tree: `/home/tjiang/repos/project_magnolia-v2`
Branch: `exp/opencode-v2`

> Status after fixes: the defects in this assessment are fixed in the working
> tree. Full suite: 219 passed; campaign suite: 152 passed; memory suite: 690
> passed; compchem-tools suite: 325 passed, 3 skipped; ruff clean. A harmless
> real local job passed submission, polling, gate, receipt, and core-hour
> accounting. A real local pause/resume reused the same run ID without a second
> submission. Pause/resume identity, status-before-timeout, campaign-owned
> retry tags, JSON-aware distillation redaction, and pause-origin recovery are
> also fixed. Remaining validation: a real Slurm job and a real
> independent reviewer. Segment-ID and scoring-scheme checks remain best-effort.

## Verdict

The second-pass implementation is much stronger. It is still not ready for
unattended multi-job campaigns or HADDOCK production runs.

The single-worker smoke path is usable for supervised development. The current
suite passes 204 tests, including 137 campaign tests. Those tests do not cover
the failures below.

Do not create an Nle protocol during this remediation. The sequence, reference
peptide, construct termini, and experimental oracle are still unresolved.

## What is now solid

- Queued-stage compare-and-swap prevents stale runners from starting old stages.
- Supervised stop paths handle HALT, Ctrl-C, SIGTERM, and process-group cleanup.
- Real local-success and nested infrastructure-retry records are parsed correctly.
- Core hours are tracked per attempt instead of against campaign totals.
- JSON secret redaction covers keys, state, journal events, and job logs.
- Receipts include run IDs, command hashes, and job usage.
- Hard-stage reviewer model selection uses the actual worker model.
- Dedicated-service database identity is checked.
- A live auto-started-service worker completed and wrote a complete receipt.

## High-priority defects

### 1. Attempt ownership ends before polling and validation

Evidence:

- `campaign_runner/runner.py:154` handles `waiting_job` without an attempt lease.
- `campaign_runner/runner.py:206` handles `validating` without an attempt lease.
- `campaign_runner/runner.py:617` validates without checking the current stage
  and attempt under the campaign lock.

Two runners can validate the same attempt. They can run the gate twice, overwrite
receipts, and issue conflicting transitions. A restart during validation has the
same risk.

Required fix:

- Keep attempt ownership through job polling and validation, or reacquire and
  verify it before those phases.
- Add compare-and-swap checks to job completion and final validation transitions.
- Do not run a gate until the current stage, attempt, and owner are confirmed.

Required tests:

- `test_second_runner_cannot_validate_same_attempt`
- `test_second_runner_cannot_complete_same_job_attempt`
- `test_restart_during_validation_requires_lease_recovery`
- `test_stale_validation_cannot_overwrite_receipt`

### 2. Failed jobs leave sibling jobs running

Evidence:

- `campaign_runner/jobs.py:438` returns on the first failed job without cancelling
  or superseding the other jobs.

Probe with one infrastructure failure and one running sibling:

```text
outcome: terminal infra
cancelled siblings: []
states: parent=submitted, nle=submitted
```

A retry can then run beside the old attempt. Partial submission has the same
cleanup problem.

Hash validation also happens inside the submission loop at
`campaign_runner/jobs.py:276`. An early job can be submitted before a later hash
mismatch is discovered.

Required fix:

- Precompute and validate every command hash before any submission.
- On terminal failure, cancel or mark every sibling in that attempt as superseded.
- On partial submission failure, clean up already submitted jobs before returning.
- Count active jobs across attempts when enforcing `max_parallel_jobs`.

Required tests:

- `test_failed_job_cancels_running_siblings`
- `test_infra_retry_has_no_live_previous_attempt`
- `test_partial_submission_failure_cleans_up_submitted_jobs`
- `test_hash_mismatch_causes_zero_submissions`
- `test_parallel_limit_counts_old_active_attempts`

### 3. Plain Slurm and PBS jobs are accepted but never polled

Evidence:

- The job backend accepts `scheduler="slurm"` and `scheduler="pbs"`.
- `mcp-servers/compchem-tools/src/compchem_tools/tools/poller.py:497` only scans
  `ssh-slurm` and `local` records.

A plain Slurm probe returned no active records:

```text
plain slurm records polled: []
```

These campaigns will wait until timeout.

Required fix:

- Implement the full plain-Slurm/PBS lifecycle, or reject those schedulers during
  protocol and job-template validation.
- Do not accept a scheduler that the polling backend cannot track.

Required tests:

- `test_unsupported_scheduler_rejected_before_submit`
- `test_supported_scheduler_has_matching_poller`

### 4. Missing `tool` bypasses HADDOCK pre-job checks

Evidence:

- `campaign_runner/prejob.py:42` runs HADDOCK checks only when
  `tool == "haddock3"`.

The same invalid fixture gave:

```text
tool missing  → ACCEPTED
tool haddock3 → REJECTED: fle_end_2 does not match last residue
```

Required fix:

- Require an explicit supported tool in every job template, or infer HADDOCK from
  `config.cfg` with an explicit validation rule.
- Fail closed when the tool is unknown and a HADDOCK config is present.

Required tests:

- `test_missing_tool_with_haddock_config_rejected`
- `test_unknown_tool_cannot_bypass_validator`

## Other defects

### 5. Local core-hour accounting is blind

A successful local job with `ncores: 8` returned `core_hours: 0.0`.
`campaign_runner/jobs.py:621` only reads `remote.slurm.elapsed`, which local jobs
do not have.

`max_core_hours` therefore does not bound local compute.

Required fix:

- Record start and end times for local jobs.
- Compute elapsed time from the local run record or process metadata.
- Test running and completed local jobs.

### 6. Malformed startup approval cannot be repaired through the CLI

Evidence:

- `start_approved()` correctly returns false for an invalid record.
- `campaign_runner/state.py:682` rejects any existing file as already approved.

Probe result:

```text
start_approved: False
approval blocked: startup is already approved
```

Required fix:

- Refuse execution only when `start_approved()` returns true.
- Allow a valid approval to replace a malformed or stale approval record.
- Keep the protocol-hash check.

Required test:

- `test_malformed_start_approval_can_be_repaired`

### 7. Distillation destroys hashes and is not scheduled

Evidence:

- `mcp-servers/compchem-memory/src/compchem_memory/opencode_ingest.py:35`
  redacts every long hexadecimal string.
- A direct probe produced `sha256=[REDACTED_SECRET]`.

This contradicts the requirement to preserve evidence hashes. The campaign queue
now has a consumer, but it only runs when the operator invokes `distill-queue`.

Required fix:

- Remove the generic long-hex rule from the distillation scrubber.
- Keep explicit secret shapes and key-aware redaction.
- Add a regression test with a secret, SHA-256, run ID, and session ID.
- Schedule queue draining after terminal stages if always-on learning is required.
  Do not run distillation during an active gate.

## Remaining validation limits

- No real Slurm acceptance job has passed.
- No real independent reviewer smoke has passed.
- HADDOCK segment-ID and scoring-scheme checks remain best-effort.
- Remote cancellation is requested but not independently confirmed.

## Recommended implementation order

1. Extend attempt ownership through polling and validation.
2. Add sibling cleanup and prevalidate every job before submission.
3. Reject unsupported schedulers and missing job tools.
4. Add local elapsed-time accounting.
5. Repair malformed approvals and preserve hashes during distillation.
6. Run the new race and multi-job tests.
7. Run real local, real Slurm, and real reviewer acceptance tests.
8. Reassess before any Nle protocol or unattended campaign.

No source files were changed during the assessment itself.
