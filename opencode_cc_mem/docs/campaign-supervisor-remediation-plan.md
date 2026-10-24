# Campaign supervisor remediation plan

> Status 2026-10-24 (second pass): the assessed race, lifecycle, accounting,
> redaction, pre-job, receipt, and approval defects are fixed. Full test suite:
> 219 passed; campaign suite: 152 passed; memory suite: 690 passed;
> compchem-tools suite: 325 passed, 3 skipped; ruff clean. A live
> auto-started-service worker completed successfully and wrote a complete
> receipt. A harmless real local job passed submission, polling, gate, receipt,
> and core-hour accounting. A real local pause/resume reused the same run ID
> without a second submission. The campaign memory queue has an explicit
> `distill-queue` consumer and an optional `--distill-on-complete` switch.
> Remaining live validation: a real Slurm job and a real independent reviewer
> model. Pre-job segment-ID and scoring-scheme checks are best-effort, not a
> substitute for the HADDOCK validator. No Nle protocol has been created.

Date: 2026-10-24.
Audience: the model or engineer implementing the correction.
Target tree: `~/repos/project_magnolia-v2`, branch `exp/opencode-v2`.

This document describes how to correct the current campaign supervisor. It is
based on a full review of the implementation and the build plan. Preserve the
working parts. Do not replace them with a new framework.

## 1. Current assessment

The current implementation has a real happy path:

- strict protocol validation;
- campaign state, journal, locks, and approval;
- opencode v2 worker execution and JSON parsing;
- optional reviewer with evidence-path validation;
- deterministic script and gate execution;
- job templates, polling, retries, cancellation, and accounting;
- a live smoke where one real worker wrote output and completed a campaign;
- 151 passing tests and clean ruff.

It is not ready for unattended scientific campaigns. The main gaps are stop
semantics, attempt locking, command-hash verification, real job polling, startup
approval, complete pre-job checks, and secret redaction.

Do not create an Nle campaign protocol in this remediation. The exact peptide
sequence, reference peptide, construct termini, and experimental oracle remain
unresolved. A protocol would imply validation that does not exist.

## 2. Non-negotiable acceptance criteria

The remediation is complete only when all are true:

1. `HALT`, CLI `halt`, budget stop, and `KeyboardInterrupt` stop active processes
   promptly and cancel their recorded jobs.
2. A completed worker cannot advance a halted or halting campaign.
3. Two runner processes cannot execute the same stage attempt or submit the same
   job twice.
4. Reusing a run record with a different command hash is rejected.
5. The real job lifecycle works without depending on an undocumented external
   poller.
6. No campaign starts without an explicit startup approval.
7. `protocol.project` and the actual project directory agree.
8. Every attempt, including failed submission and failed polling, has a receipt.
9. All pre-job checks in `rules/prejob_check.md` are implemented or explicitly
   delegated to an existing, verified validator.
10. Raw logs and distillation inputs contain no known secret shapes, while
    hashes and run IDs remain intact.
11. No production path can use a fake job backend.
12. Existing tests still pass, and the new safety tests in section 8 pass.
13. One live worker smoke and one harmless real Slurm job pass.

## 3. Required architecture changes

### 3.1 Add an attempt lease, not only a state lock

The current `transition()` lock protects only a state write. It does not protect
execution. Add a lease per stage attempt:

```text
control/leases/<stage>-<attempt>.json
```

Suggested contents:

```json
{
  "stage": "prepare",
  "attempt": 1,
  "owner_pid": 12345,
  "owner_pgid": 12345,
  "opencode_session_id": null,
  "hostname": "workstation",
  "started_at": "<utc>",
  "heartbeat_at": "<utc>"
}
```

Rules:

- Create the lease atomically with `O_CREAT | O_EXCL`.
- If a live lease exists, raise `CampaignBusyError`. Do not start a second
  attempt and do not call it an interrupted run.
- Heartbeat every five seconds while the attempt is active.
- Release the lease only after a terminal receipt is written or the attempt is
  stopped and recorded.
- On recovery, read the owner PID before deciding anything:
  - owner alive: refuse to start another attempt;
  - owner dead and lease stale: mark the attempt interrupted and require human
    review before any retry;
  - no lease owner: do not guess that the attempt is safe to rerun.
- Keep `CampaignStore.lock()` for state writes. The lease is for execution.

Add `control/attempt-index.json` so attempt directories and lease IDs are stable
across restarts.

### 3.2 Replace blocking process waits with supervised execution

The current `run_process()` blocks until completion. `HALT` is noticed only
between stages. Replace it with a monitor loop:

```text
start Popen(start_new_session=True)
record pid, pgid, attempt, and stdout/stderr paths
while process.poll() is None:
    if stop_requested(): terminate process group, record, return
    if budget_exceeded(): terminate process group, record, return
    if timeout_exceeded(): terminate process group, record, return
    heartbeat()
    sleep(0.2 seconds)
read and parse final output
```

Use SIGTERM first, then SIGKILL after a bounded grace period. Keep the process
group so children are stopped too.

Every state transition after process completion must first check for an active
stop condition. If a stop was requested, do not validate or advance. Write the
stop receipt instead.

Required stop paths:

- `HALT` file;
- CLI `halt`;
- token, core-hour, cost, or wall budget stop;
- stage or job timeout;
- `KeyboardInterrupt` in `run_forever`;
- service or daemon shutdown.

Do not swallow cancellation errors. If any process or job cannot be stopped,
record the error and do not report a clean halt. Use a `halting` state until all
stop actions are confirmed. Move to `failed` if confirmation is impossible.

### 3.3 Define the real job lifecycle

The current backend only reads YAML. It never invokes the poller. Use the
existing lifecycle explicitly.

For each `waiting_job` pass:

1. Call `compchem_tools.tools.poller.poll_jobs(project_dir)` in the project
   virtual environment. This checks terminal states, fetches results, and runs
   assessment.
2. Read the resulting run record with `CompchemJobBackend.status`.
3. Only then transition to `validating` or failure handling.

If `poll_jobs` raises or returns an error, record it as pending with an error
counter. Do not convert a transient poller error into a scientific failure.
Apply the stage job timeout and cancel active jobs on timeout.

If this API cannot be used, implement the equivalent explicit sequence:
`check_job` -> terminal `fetch_job_results` -> `post_run_assess` -> read YAML.
Do not silently require a separate daemon to be running.

### 3.4 Enforce command-hash identity

At `jobs.py:248-250`, an existing record with a `run_id` is currently reused
without comparison. Change it:

- Compute `_canonical_hash(request)` before inspecting the existing record.
- If an existing record has a different `command_sha256`, raise `JobError` with
  the key, old hash, and new hash. Never resubmit or reuse silently.
- If the hash matches and `run_id` exists, reuse it only when its status is
  `submitted`, `running`, or `succeeded`.
- A record in `submitting` with no run ID remains uncertain and requires human
  review. Do not auto-retry it.

The hash must cover command, working directory, and submit parameters.

### 3.5 Require startup approval and project identity

A protocol is not permission to execute. Add explicit startup approval:

- `init` creates the campaign in `waiting_human` and writes a startup request.
- The first execution is allowed only after `approve` records a startup approval.
- If the first stage is `human`, use that gate as the startup approval.
- `approve` for a non-human first stage starts stage 1. It does not skip it.

Also enforce:

- Protocols load only from `campaigns/` or from the project’s approved campaign
  directory.
- `protocol.project` must equal the actual project directory name.
- The run directory must be inside `<project>/runs/`.
- A protocol whose project differs from the run project is rejected before any
  worker starts.

### 3.6 Implement the complete pre-job verification contract

`CompchemJobBackend.submit` currently delegates directly to `submit_job`. Add a
validator registry with a generic validator and a HADDOCK3 validator. Reuse
existing gates where they exist. Do not duplicate a correct existing check.

Universal checks:

- every referenced input exists and is non-empty;
- structure validation for every PDB/SDF;
- no symlink escapes the run directory;
- config parses, and `run_dir = "output"` is relative where required.

HADDOCK3 checks:

- chain ID and segment ID match the config;
- peptide first/last residue matches `fle_sta_2` and `fle_end_2`;
- actpass and config agree with the actual PDB;
- actpass has exactly two lines: active and passive;
- copied `ambig.tbl` residue numbers agree with actpass;
- scoring weights match the intended scheme.

Fail closed. A broken validator blocks submission. Do not use `acknowledge=True`
as a hidden bypass in normal campaign execution.

### 3.7 Redact secrets without destroying evidence

`redact_secrets` is currently unused, and its long-hex rule would also erase
SHA-256 values. Replace it with a narrow function and apply it at write time.

Redact:

- `sk-...` and `olp_...` tokens;
- `Bearer ...` credentials;
- `api_key`, `apiKey`, `token`, `secret`, and password assignments;
- provider-specific credentials with suffixes, such as `DEEPSEEK_API_KEY_MIMOSA`.

Do not redact:

- SHA-256 hashes;
- run IDs;
- file paths;
- ordinary session IDs.

Apply this to raw stdout, stderr, JSONL, receipts, and any text sent to
distillation. Add a test that proves a secret is removed while a SHA-256 remains.

### 3.8 Receipt every attempt, including job failures

Write `receipt.json` for every stage attempt, including:

- submission failure;
- poller failure;
- job timeout;
- gate failure;
- reviewer failure;
- operator stop.

Required fields:

- stage and attempt;
- status;
- session ID when applicable;
- run IDs when applicable;
- output paths and SHA-256 where present;
- error/reason;
- usage;
- command hashes;
- gate result and reviewer result where present.

### 3.9 Complete memory and accounting

- Compute `memory_state.entries_sha256` over the project’s current entry files
  with relative paths and content bytes. Record it in `campaign.json`.
- Record completed worker session IDs in a stage-completion queue for the normal
  memory pipeline. Do not build a second distiller. Do not run it during an
  active gate.
- Use the v2 DB usage reader for reviewer sessions too, with event usage as a
  fallback. Do not count usage twice.
- Add `max_cost_usd` to `Budget` and enforce it when configured and positive.
  Do not invent a dollar cost for subscription models whose recorded cost is 0.
- Track active human minutes only through an explicit approval field such as
  `--minutes`. Waiting time is not human work and must not be reported as such.
- Add a job-wait timeout using the stage timeout. On timeout, cancel active jobs
  and apply the stage’s retry policy.

### 3.10 Correct `hard_stage_model` semantics

The field is currently validated but unused. Define and test this selection
order:

1. `stage.model`, when present;
2. `worker.hard_stage_model`, only when `stage.hard: true`;
3. `worker.model`, the routine default.

Add optional `hard: boolean` to the opencode stage schema. Unknown fields remain
rejected. Reviewer model-family selection must use the final worker model.

### 3.11 Keep the fake backend out of production

`--job-backend fake` must be rejected unless `MAGNOLIA_TESTING=1`. Tests must not
set that variable in production.

Any fake result must be visibly marked as a test double in receipts and logs.

### 3.12 Manage the dedicated v2 service

The code currently accepts only an existing `--server` or environment value. Add
a small per-project service registry:

```json
{
  "project": "hsc70_new",
  "port": 4112,
  "db": "/home/tjiang/.local/share/opencode/opencode-v2-hsc70.db",
  "pid": 12345,
  "started_at": "<utc>"
}
```

Provide `ensure`, `status`, and `stop` operations:

- start `opencode serve --hostname 127.0.0.1 --port <port>` with the project DB;
- probe the actual v2 health endpoint once, record it, and require a healthy
  response before starting workers;
- attach workers to that service;
- never kill the shared service on campaign stop;
- stop the project service only on explicit service-stop or controlled shutdown.

If this is not implemented, a clear error is required when no healthy dedicated
service exists. Do not silently fall back to the global service.

## 4. Required state additions

Add these states if needed for correct stop behavior:

```text
halting
```

`halting` is not a successful end state. It means stop was requested but process
or job cancellation is not fully confirmed. Transition to `halted` only after
confirmation. Move to `failed` if confirmation fails.

Add these records:

- `control/leases/<stage>-<attempt>.json`
- `control/approvals/start.json`
- `control/distill-queue.jsonl`
- `control/job-errors.jsonl`
- `control/cancel-errors.jsonl`

Every record needs an atomic write path and a reader that rejects malformed
content.

## 5. Files to change

| File | Required work |
|---|---|
| `process.py` | Supervised execution, process-group stop, PID/PGID metadata |
| `runner.py` | Attempt lease, stop-before-transition, poller invocation, receipts, budget checks |
| `jobs.py` | Hash comparison, lifecycle, failed receipts, cancellation error records |
| `state.py` | Startup approval, lease recovery, halting state, memory hash |
| `models.py` | `hard`, `max_cost_usd`, job timeout, allowed protocol roots |
| `workers.py` | Redacted logs, no stop override, config-content merge preserved |
| `reviewers.py` | DB usage fallback and redacted logs |
| `security.py` | Narrow secret patterns, no hash destruction, path restrictions |
| `cli.py` | Startup approval, service lifecycle, test-only fake guard, interrupt cancellation |
| `.opencode/agents/campaign-worker.md` | Preserve restricted write paths; add untrusted-data rule |
| `.opencode/agents/campaign-reviewer.md` | Preserve read-only behavior and untrusted-data rule |
| `gates.py` | Receipt completeness and stop-before-gate handling |

Do not rewrite the good schema, event parser, gate runner, or approval model
without a demonstrated defect.

## 6. Required unit tests

Add these exact tests, in addition to the existing suite:

### Stop and concurrency

1. `test_halt_kills_worker_midflight`
   - Worker sleeps with a child process.
   - Request halt.
   - Assert process group is gone, state is `halted`, and no gate starts.

2. `test_worker_completion_cannot_override_halt`
   - Halt while the worker is active.
   - Let the worker finish afterward.
   - Assert no transition to `validating` or `completed`.

3. `test_keyboardinterrupt_cancels_jobs_and_processes`
   - Raise `KeyboardInterrupt` in the loop.
   - Assert job cancellation and a stop receipt, not just exit 130.

4. `test_second_runner_cannot_execute_same_attempt`
   - Hold a live lease.
   - Start another runner.
   - Expect `CampaignBusyError` and zero new submissions.

5. `test_dead_lease_requires_human_review`
   - Kill the owner process, leave a stale lease.
   - Assert `waiting_human`, not an automatic retry.

### Job integrity

6. `test_command_hash_mismatch_rejected`
   - Reuse a run ID after changing the command.
   - Expect `JobError`, not reuse.

7. `test_poller_is_called_before_status`
   - Mock `poll_jobs` and assert it is called before every terminal-status read.

8. `test_poller_error_does_not_create_science_failure`
   - Poller raises once, then succeeds.
   - Expect pending, then normal validation.

9. `test_failed_submission_and_failed_poll_write_receipts`
   - Assert both contain run IDs when available, reason, and command hash.

10. `test_job_wait_timeout_cancels_jobs`
    - Make a job exceed its stage timeout.
    - Assert cancellation and no automatic retry unless explicitly allowed.

### Approval and project security

11. `test_nonhuman_first_stage_requires_start_approval`
    - Run without approval.
    - Assert `waiting_human` and no worker execution.

12. `test_protocol_project_mismatch_rejected`
    - Use a protocol from another project.
    - Expect rejection before any model starts.

13. `test_protocol_outside_allowed_root_rejected`
    - Load from an arbitrary directory.
    - Expect rejection.

14. `test_fake_backend_rejected_in_production`
    - Call `--job-backend fake` without `MAGNOLIA_TESTING=1`.
    - Expect exit 2.

### Memory, accounting, and secrets

15. `test_memory_hash_is_recorded`
    - Change an entry file after initialization.
    - Assert hash mismatch on the next verification.

16. `test_reviewer_uses_db_usage`
    - Put reviewer usage in `session_message`.
    - Assert it is counted once.

17. `test_secrets_redacted_but_hashes_preserved`
    - Include a secret, a SHA-256, a run ID, and a path.
    - Assert only the secret is removed.

18. `test_cost_budget_stop`
    - Set `max_cost_usd` below a known usage.
    - Assert stop and cancellation.

19. `test_human_minutes_are_not_wait_time`
    - Assert waiting time does not increment human work minutes.
    - Assert explicit `--minutes` is recorded if supplied.

### Prompts and gates

20. `test_hard_stage_model_selection`
    - Cover routine default, `hard: true`, and explicit `stage.model`.

21. `test_untrusted_text_cannot_change_protocol`
    - Put an instruction in retrieved content.
    - Assert it cannot create a stage, approval, or command.

22. `test_prejob_haddock_residue_mismatch_rejected`
    - Use actpass and PDB ranges that disagree.
    - Assert submission is blocked before the backend call.

23. `test_prejob_symlink_escape_rejected`
    - Use a symlinked input outside the run directory.
    - Assert rejection.

## 7. Required integration tests without live models

Use fake workers and a fake job backend only when `MAGNOLIA_TESTING=1`:

- full success through script, worker, reviewer, and job stages;
- gate failure with one repair;
- reviewer blocker followed by human approval;
- restart while jobs are pending;
- one infrastructure retry;
- one hard budget stop;
- one operator stop with active jobs;
- second runner refused.

Assert state, event order, receipts, output hashes, and job counts. Fake results
must be marked as test doubles.

## 8. Required live acceptance tests

### 8.1 Worker smoke

Use a real opencode v2 worker on a disposable project. Require it to write
`work/out.md`, pass the gate, and complete. Record token and cost usage.

### 8.2 Real Slurm smoke

Use a one-core, short `hostname > hostname.txt` job. Verify:

- submission through `submit_job`;
- `poll_jobs` is called;
- the run record reaches `fetched`;
- the output file is found by the gate;
- restart during the wait does not duplicate the job;
- `halt` cancels the active job.

Do not use docking for this test.

### 8.3 Real reviewer smoke

Use one valid report and one deliberately overclaiming report. The reviewer
must flag the overclaim with evidence paths. Valid JSON must not advance the
stage when a blocker exists.

Use direct `deepseek/deepseek-flash` for the first test unless the Go credential
is configured in the v2 DB. Do not require K3.1 for the smoke.

### 8.4 Nle pilot

Run only after all previous tests pass and the construct questions are resolved.
Use one parent/variant pair, not eleven runs. Require same-mode analysis before
score comparison and no experimental claim beyond available evidence.

## 9. Suggested implementation order

1. Attempt lease and supervised stop behavior.
2. Command-hash identity and real job polling.
3. Startup approval, project identity, and fake-backend guard.
4. Full pre-job validators.
5. Receipts for all failures and secret redaction.
6. Memory hash, reviewer accounting, budget additions, and job timeout.
7. Dedicated service management.
8. Unit and integration tests.
9. Live smoke tests.
10. Only then consider the Nle protocol.

Keep each step independently reviewable. Do not commit unless the user asks.

## 10. Explicit handoff instructions to the implementing model

- Read this document, the current build plan, and the current implementation.
- Preserve working code unless a defect is shown.
- Do not use a fake backend in production or in live acceptance tests.
- Do not mark a stage complete from a text response alone. Always run the gate.
- Do not swallow cancellation, polling, or validation errors.
- Do not create an Nle protocol or submit Nle jobs in this remediation.
- Do not claim a live test passed without naming the command, artifacts, and
  measured evidence.
- Run ruff and the complete relevant pytest suite after every phase.
- Report changed files, tests, remaining gaps, and any observed behavior that
  contradicts this document.
