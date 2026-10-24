# Campaign supervisor on opencode v2: detailed build plan

Status: draft for implementation, 2026-10-23.
Audience: the agent implementing this plan and the human reviewer.
Target tree: `~/repos/project_magnolia-v2`, branch `exp/opencode-v2`.
Pinned runtime: opencode v2.0.6 at `~/.local/share/magnolia-v2/bin/opencode`.

This plan adds partial automation to Magnolia. It does not attempt open-ended
scientific discovery. A human approves the protocol and the scientific claims.
The supervisor executes the protocol and handles routine waiting and recovery.

## Implementation status (2026-10-24)

Implemented and tested:

- protocol validation with per-stage model routing;
- campaign state, journal, locks, approvals, and reconciliation;
- opencode v2 worker execution and observed JSON parsing;
- optional independent reviewer with evidence-path validation;
- deterministic script and gate execution with process-group timeout;
- job templates, submission, polling, retries, cancellation, and usage;
- token, cost, and core-hour accounting;
- CLI commands for validate, init, status, approve, pause, resume, halt,
  reconcile, run, and daemon.

A live smoke on 2026-10-24 ran one real opencode v2 worker through the new
supervisor. The worker wrote `work/out.md`, the gate passed, and the campaign
completed. Usage was recorded as 2,252 input, 217 output, 42 reasoning, and
3,584 cache-read tokens, with cost 0.000504 USD.

Not created yet:

- a real Nle campaign protocol. The exact peptide sequence, reference peptide,
  construct termini, and experimental oracle are still unresolved. Creating a
  launchable protocol now would imply validation that does not exist.
- adaptive follow-up execution. The schema is described here, but the first
  version records follow-ups only from the reviewer and does not execute them.

## 1. Decisions already made

1. Build an always-on supervisor, not a permanent chat session.
2. Keep the existing mailbox daemon for short letters. Add a separate campaign
   runner. Do not change mailbox behavior in the first implementation.
3. A campaign is a bounded job with a brief, stages, artifacts, budget, and a
   defined end.
4. Use a fresh opencode session for each worker attempt. Do not resume one lane
   session across unrelated stages or campaigns.
5. The supervisor contains no LLM. It owns state, locks, budget, and transitions.
6. Workers produce plans, patches, and reports. They do not certify completion.
7. Deterministic gates control stage advancement. An optional LLM reviewer may
   criticize work and propose follow-ups, but it cannot approve work or change
   the protocol.
8. Long computations go through `submit_job` and durable run records.
9. Start with one fixed protocol: the existing Nle paired-docking runbook.
10. Add adaptive follow-ups only after the fixed protocol works end to end.
11. Route models by stage. Use K3.1 Pro only for hard scientific reasoning, use
    DeepSeek-V4.1-Flash for routine work and review, and keep GLM-5.3 as the
    independent reviewer alternative. Do not use the strongest model everywhere.

## 2. Objective and non-goals

### 2.1 Objective

After a human approves a campaign, the supervisor should be able to:

- execute fixed script, worker, and Slurm stages;
- wait for tracked jobs without further human prompts;
- validate required artifacts with deterministic checks;
- optionally request an independent LLM review;
- retry only approved infrastructure failures;
- pause for scientific ambiguity, insufficient evidence, or approval;
- resume after a daemon restart without duplicate work;
- stop workers and campaign jobs on operator request;
- produce a complete record of decisions, artifacts, usage, and limitations.

### 2.2 Non-goals for the first implementation

- Open-ended research or arbitrary task creation.
- A permanent opencode conversation.
- Automatic merging, publishing, memory promotion, or harness modification.
- Multiple worker roles competing for judgment.
- A web interface.
- A new memory database or a general knowledge graph.
- Unattended runs longer than 72 hours.
- Direct execution of arbitrary shell text supplied by a worker.

## 3. Glossary

- **Campaign:** one bounded research job.
- **Supervisor / campaign daemon:** the same deterministic background process.
- **Stage:** one step in the campaign protocol.
- **Attempt:** one execution of a stage. A stage may have several attempts.
- **Worker:** an opencode v2 session that performs one stage attempt.
- **Reviewer:** an optional, separate opencode session that criticizes artifacts.
- **Gate:** a deterministic check that returns pass or fail.
- **Oracle:** an independent ground-truth check, such as a known answer, a
  held-out dataset, a second method, or an experimental assay.
- **Receipt:** the machine-readable record of one attempt and its checks.
- **Follow-up:** a proposed new test triggered by an observation.

## 4. Architecture

```text
approved protocol
       |
       v
campaign daemon
  - reads state
  - enforces locks and budget
  - starts one stage attempt
       |
       +--> fixed script stage
       |
       +--> fresh opencode worker
       |         |
       |         +--> optional fresh LLM reviewer
       |
       +--> pre-registered submit_job command
       |         |
       |         +--> existing poller and run YAML
       |
       v
required artifacts and receipts
       |
       v
deterministic gate
       |
       +--> pass: next stage
       +--> approved retry: retry with recorded reason
       +--> ambiguity or approval required: waiting_human
       +--> budget stop or fatal error: halted/failed
```

The campaign daemon must not contain an LLM call. Only workers and reviewers
are model sessions.

### 4.1 Authority boundaries

- The daemon advances state only after the deterministic gate passes.
- A worker cannot edit the protocol, gates, budget, or campaign state.
- A reviewer cannot approve, reject, or advance a stage by text alone.
- A reviewer cannot change the protocol or submit work.
- The deterministic gate cannot judge scientific novelty or function.
- Scientific claims beyond the measured evidence require human approval.
- The daemon never promotes memory or changes shared rules.

## 5. Files and code layout

Add new code rather than growing `magnolia-agents-daemon` into a second,
incompatible runner.

```text
opencode_cc_mem/
  campaigns/
    nle-paired-pilot/
      protocol.json
      brief.md
      prompts/
      scripts/
      checks/
  campaign_runner/
    __init__.py
    models.py          # validated dataclasses and protocol schema
    state.py           # atomic state, journal, locks, transitions
    runner.py          # due-campaign loop and stage dispatch
    workers.py         # opencode process handling and output parsing
    reviewers.py       # optional LLM review sessions and review parsing
    jobs.py            # submit_job mapping, wait, reconcile, cancel
    gates.py           # artifact checks and deterministic gate execution
    accounting.py      # v2 token usage and run core-hours
    security.py        # command, path, sender, and permission checks
    cli.py             # command-line interface
  softwares/bin/
    magnolia-campaign  # thin executable calling campaign_runner.cli
  tests/
    test_campaign_schema.py
    test_campaign_state.py
    test_campaign_runner.py
    test_campaign_workers.py
    test_campaign_jobs.py
    test_campaign_reviewers.py
    test_campaign_security.py
    test_campaign_accounting.py
    test_nle_gates.py
    test_campaign_live_v2.py
```

Use Python standard library for the core runner. Keep model and job execution
behind small interfaces so tests can replace them without network calls.

## 6. Campaign files

A protocol template lives under `campaigns/<name>/` and is version controlled.
Starting a campaign copies the protocol into a run directory and records its
hash.

```text
projects/hsc70_new/runs/YYYY-MM-DD_nle-paired-pilot/
  control/
    brief.md                 # frozen human-approved brief
    protocol.json            # frozen copy of the protocol
    campaign.json            # current state, written only by the daemon
    scripts/                 # controller-owned executable commands
    checks/                  # controller-owned gates
    approvals/               # human approval records
  work/                      # the only worker-writable directory
  attempts/
    001-patch-nle/
      worker.jsonl
      reviewer.json
      receipt.json
  jobs.json                  # stage attempt -> run IDs and commands
  events.jsonl               # append-only transition journal
  reports/
  HALT                       # optional operator stop marker
  PAUSE                      # optional operator pause marker
```

Use atomic write-then-rename for JSON state files. Use `fcntl.flock` for the
campaign lock. One active campaign per project is enough for the first version.
Several independent Slurm jobs may run in parallel within a job stage.

## 7. Protocol and state schemas

### 7.1 `protocol.json`

Reject unknown top-level fields and unknown stage types.

```json
{
  "schema": 1,
  "id": "nle-paired-pilot-v1",
  "project": "hsc70_new",
  "worker": {
    "model": "opencode-go/deepseek-v4.1-flash",
    "hard_stage_model": "kimi-code-plan-cn/k3.1-pro#high",
    "agent": "campaign-worker",
    "max_steps": 30,
    "timeout_seconds": 1800
  },
  "reviewer": {
    "enabled": true,
    "model": "opencode-go/deepseek-v4.1-flash",
    "alternate_model": "zai-coding-plan/glm-5.3",
    "max_steps": 8,
    "timeout_seconds": 600,
    "repair_attempts": 1
  },
  "budget": {
    "max_tokens": 2000000,
    "max_wall_hours": 24,
    "max_core_hours": 40,
    "max_attempts_per_stage": 2,
    "max_parallel_jobs": 2,
    "max_followups": 0
  },
  "stages": [
    {"id": "confirm_inputs", "type": "human"},
    {"id": "backup_topology", "type": "script"},
    {"id": "patch_nle", "type": "opencode", "review": false},
    {"id": "build_and_validate", "type": "opencode", "review": false},
    {"id": "paired_docking", "type": "job"},
    {"id": "analyze", "type": "opencode", "model": "kimi-code-plan-cn/k3.1-pro#high", "review": true},
    {"id": "final_review", "type": "human"}
  ]
}
```

Model routing is explicit. `worker.model` is the cheap default for routine
stages. An `opencode` stage may override it with `worker.hard_stage_model` or
another explicit model. The example reserves K3.1 Pro for final analysis.
`reviewer.alternate_model` is used for model-family separation and ablation,
not as an automatic fallback. Never fall back to the global default model.

For a decisive review, the reviewer must be from a different model family than
the worker. A Kimi worker uses the DeepSeek reviewer. A DeepSeek worker uses the
GLM reviewer. Same-family review is allowed only for non-decisive checks.

Each stage may define:

- `inputs`: required files or previous stage IDs.
- `outputs`: required output paths and optional JSON schemas.
- `command`: a fixed argv list for `script` or a template ID for `job`.
- `prompt`: path to the worker prompt template.
- `model`: optional explicit model override for an `opencode` stage.
- `gate`: fixed argv list for the deterministic check.
- `retry`: `none`, `infra_once`, or `repair_once`.
- `review`: whether the optional reviewer runs.
- `on_gate_fail`: `repair`, `human`, `failed`, or `halt`.
- `timeout_seconds`: stage-specific override.

### 7.2 `campaign.json`

```json
{
  "schema": 1,
  "protocol_id": "nle-paired-pilot-v1",
  "protocol_sha256": "<sha256>",
  "state": "queued",
  "stage": "confirm_inputs",
  "attempt": 0,
  "jobs": {},
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "reasoning_tokens": 0,
    "core_hours": 0,
    "human_minutes": 0
  },
  "memory_state": {
    "project": "hsc70_new",
    "entries_sha256": "<optional reproducibility hash>"
  },
  "created_at": "<utc>",
  "updated_at": "<utc>"
}
```

### 7.3 `events.jsonl`

Append one JSON object per transition:

```json
{
  "seq": 12,
  "ts": "<utc>",
  "campaign": "nle-paired-pilot-v1",
  "stage": "paired_docking",
  "attempt": 1,
  "from": "running",
  "to": "waiting_job",
  "reason": "two run IDs recorded",
  "session_id": null,
  "run_ids": ["haddock3_parent", "haddock3_nle"]
}
```

### 7.4 `receipt.json`

```json
{
  "stage": "analyze",
  "attempt": 1,
  "session_id": "ses_...",
  "status": "passed",
  "outputs": [
    {"path": "work/analysis.md", "sha256": "<sha256>", "bytes": 10240}
  ],
  "checks": [
    {"name": "required_outputs", "result": "pass"},
    {"name": "paired_metrics", "result": "pass"},
    {"name": "validation_level", "result": "pass"}
  ],
  "usage": {"input": 0, "output": 0, "reasoning": 0},
  "review": {"path": "attempts/001-analyze/reviewer.json"}
}
```

### 7.5 `jobs.json`

Record enough information to prevent duplicate submission:

```json
{
  "paired_docking/1/parent": {
    "run_id": "haddock3_...",
    "command_sha256": "<sha256>",
    "submitted_at": "<utc>",
    "status": "submitted"
  },
  "paired_docking/1/nle": {
    "run_id": "haddock3_...",
    "command_sha256": "<sha256>",
    "submitted_at": "<utc>",
    "status": "submitted"
  }
}
```

## 8. State machine

Allowed states:

```text
queued
running
waiting_job
validating
waiting_human
completed
failed
halted
paused
```

Basic transitions:

```text
queued -> running
running -> validating
running -> waiting_job
running -> waiting_human
running -> failed
running -> halted
waiting_job -> validating
waiting_job -> halted
validating -> queued            # next stage, or approved repair attempt
validating -> running           # transition record for a started repair
validating -> waiting_human
validating -> completed
validating -> failed
validating -> halted
waiting_human -> queued         # approval of a non-human stage
waiting_human -> completed      # approval of the final human stage
paused -> queued                # after explicit resume
any active state -> halted      # operator stop or hard budget stop
```

`completed` is allowed only after the final stage passes. A final human review
may be mandatory in the protocol.

Every transition writes an event before updating `campaign.json`. If the
process crashes between the two writes, reconciliation uses the journal and
receipts to reconstruct the state.

## 9. Stage execution contracts

### 9.1 `script`

- `command` is an argv array, not a shell string.
- No worker-substituted arguments are allowed in the first version.
- Run with `subprocess.Popen(..., start_new_session=True)`.
- Enforce timeout and process-group termination.
- Run the gate after checking required outputs.
- A nonzero exit never advances the stage.

### 9.2 `opencode` worker

Use one fresh session per attempt:

```bash
opencode run \
  --server http://127.0.0.1:<opencode-port> \
  --model <provider/model#variant> \
  --agent campaign-worker \
  --format json \
  --auto \
  "<rendered stage prompt>"
```

Requirements:

- Use the pinned v2 binary and the project's own `OPENCODE_DB`.
- Use a dedicated v2 service per project. Do not use the global default model.
- Do not use `--standalone` for the first implementation. Local ingestion notes
  that standalone runs leave the event table empty, which harms accounting.
- `start_new_session=True` and explicit process-group kill are mandatory.
- Save raw JSON events and stderr for every attempt.
- The v2.0.6 JSON event shape must be probed before adapting the parser. Do not
  assume the v1 `part.text` shape.
- Resume the same session only for an output-format retry. Never resume a lane
  session from a previous stage or campaign.

The prompt must name exact input and output paths. The worker may write only in
`work/`. The daemon writes the receipt after the gate runs.

### 9.3 `job`

- Select a pre-registered command template by ID.
- Verify the command hash before submission.
- Run all checks in `rules/prejob_check.md` mechanically.
- Submit through `submit_job`, never raw `ssh sbatch`.
- Store every run ID in `jobs.json` before entering `waiting_job`.
- Reconcile from durable run YAML, not only the transient notices file.
- On restart, inspect existing run IDs before submitting anything.
- Operator stop calls `cancel_job` for every active run ID.

A job stage names a template file at `control/jobs/<template-id>.json`. The file
contains a `jobs` array. Each entry has `key`, `command`, `working_dir`, and
optional `submit` arguments. Placeholders `{run_dir}`, `{project_dir}`,
`{work_dir}`, `{stage}`, and `{attempt}` are rendered by the supervisor only.
Workers cannot add templates or arguments.

### 9.4 `human`

Enter `waiting_human` and emit a short request containing:

- what is required;
- why the campaign stopped;
- which evidence to inspect;
- the exact approval command or file to provide.

Do not start another worker until approval exists. Approval is a JSON record in
`control/approvals/`, written by the CLI, not by a worker.

## 10. Optional LLM reviewer

### 10.1 Purpose

The reviewer looks for semantic problems that deterministic gates miss:

- claims that exceed the measured evidence;
- contradictions between the report and raw output;
- missed anomalies;
- alternative explanations;
- useful, bounded follow-up tests.

It is not an independent oracle and not a second supervisor.

### 10.2 Execution

Run a fresh session with read-only access to the stage artifacts. Give raw
artifacts before the worker's report to reduce anchoring. The reviewer must cite
file paths for every issue.

The daemon writes the review to `reviewer.json`. The reviewer has no shell, job,
edit, memory-promotion, or protocol-modification capability.

Use the configured model routing for decisive reviews: Kimi worker with
DeepSeek reviewer, or DeepSeek worker with GLM reviewer. `alternate_model` is
not an automatic fallback. Give the reviewer its own step and timeout budget.
Allow enough output tokens for DeepSeek's hidden reasoning and require valid
JSON with evidence paths. If no valid review arrives, enter `waiting_human`.

### 10.3 Review schema

Do not use a simple `approved: true` field. Use issue objects:

```json
{
  "issues": [
    {
      "severity": "blocker",
      "claim": "The report says Nle binds better, but the poses differ",
      "evidence": ["work/parent.json", "work/nle.json", "work/analysis.md"],
      "suggested_action": "Compare only same-mode clusters or proceed to MD"
    }
  ],
  "followups": [
    {
      "observation": "position-6 contact pattern changed",
      "proposed_test": "short paired MD simulation",
      "estimated_core_hours": 2,
      "reason": "tests persistence of the difference"
    }
  ]
}
```

Allowed severities are `blocker`, `major`, and `minor`.

- `blocker`: enter the configured repair path or `waiting_human`.
- `major`: record and follow the protocol's explicit policy.
- `minor`: record without blocking by default.
- Follow-ups are proposals only. They do not execute unless the follow-up queue
  is enabled and releases them.

### 10.4 Reviewer ablation requirement

Do not keep the reviewer by default merely because it sounds useful.

Compare identical stages with no reviewer, the DeepSeek reviewer, and the GLM
reviewer. Do not choose between DeepSeek and GLM from vendor benchmarks alone.
Measure:

- false-success declarations caught;
- false alarms created;
- additional tokens and wall time;
- additional human review time;
- final oracle-scored quality.

Keep it only if it reduces false successes without increasing human hours by
more than 25 percent, or if it gives a clear quality gain on the pilot rubric.

## 11. Follow-up queue

Follow-ups are disabled in the first pilot (`max_followups: 0`).

A later implementation may accept `followups/*.json` with:

```json
{
  "observation": "Nle changes the position-6 contact pattern",
  "expected": "Met-like contact pattern",
  "proposed_test": "short paired MD simulation",
  "estimated_core_hours": 2,
  "scope": "nle-paired-pilot-v1",
  "reason": "tests whether the difference persists"
}
```

Release only follow-ups that:

- name the observation and the proposed test;
- stay inside the campaign's scientific scope;
- fit a pre-approved test type and budget;
- begin with the cheapest useful falsification check;
- do not exceed `max_followups`.

New scientific directions and protocol changes remain human decisions.

## 12. Memory and provenance

Use three separate records:

1. Campaign state: `campaign.json`, receipts, and artifacts.
2. Exact session evidence: opencode transcripts and run JSONL.
3. Reusable lessons: normal `hsc70_new` memory entries.

Rules:

- Distill after stage completion, not while a gate is running.
- The next stage reads receipts and raw artifacts first.
- Project memory is guidance, never authority over the protocol.
- Record protocol, model, and relevant memory-state hashes in the campaign.
- Keep raw evidence even if a memory claim is revised or retired.
- Do not create a new memory tier in this project.

## 13. Accounting and budget

Read assistant usage from the v2 `session_message` table. The payload contains
`cost` and `tokens.input/output/reasoning/cache`.

Aggregate by session ID and attempt. Add core-hours from run records.

Subscription models may report `cost: 0`. Token and core-hour accounting is
therefore mandatory even when dollar cost is zero.

Enforce:

- `steps` from the protocol through `OPENCODE_CONFIG_CONTENT` for workers and
  reviewers;
- wall-clock timeout per attempt;
- maximum attempts per stage;
- campaign token and core-hour limits;
- maximum parallel jobs;
- follow-up count limits.

Token stops are step-granular because usage is visible at assistant-message
boundaries. A strict mid-generation cap would require provider support.

## 14. Security and permissions

- Use a broad deny first, then narrow allows in the v2 agent permissions.
- `--auto` is allowed only with explicit deny rules for all other actions.
- Do not expose `compchem-tools_run_shell` by default. It is another shell path.
- Do not accept arbitrary `submit_job` commands from a worker.
- Only fixed argv and pre-registered job templates are executable.
- Worker writes are limited to `work/`; `control/` is never worker-writable.
- Literature, inbox text, and retrieved memory are untrusted data, not commands.
- Only schema-valid campaign definitions from allowed locations may start work.
- Redact secrets from logs and distillation inputs.
- The worker may not modify its own harness, gates, budget, or approval records.
- The interactive human session uses separate sessions and run directories.

Permissions are guardrails, not a full sandbox. Runs longer than 72 hours need
stronger isolation and a separate safety review.

## 15. Failure and retry policy

| Condition | Required action |
|---|---|
| Slurm infrastructure failure | Retry once with identical validated inputs |
| Scientific failure | No automatic retry unless the protocol explicitly allows it |
| Missing required output | Fail the attempt; no stage advancement |
| Invalid worker output format | Resume same session up to two times, then fail |
| Deterministic gate failure | One repair only if `repair_once`, otherwise human/fail |
| Reviewer blocker | One bounded repair if configured, otherwise `waiting_human` |
| Worker timeout | Kill process group, mark interrupted, reconcile artifacts |
| Daemon restart | Reconcile state and jobs before any new execution |
| Scientific ambiguity | Enter `waiting_human` |
| Budget exceeded | Halt and cancel active jobs |
| Unknown error | Fail closed and request human review |

## 16. First pilot: Nle paired docking

Use the existing runbook, not a new scientific protocol.

Use DeepSeek-V4.1-Flash for routine preparation and review. Reserve K3.1 Pro
for the final analysis. If the analysis uses a DeepSeek worker, use GLM-5.3 for
the independent reviewer instead of DeepSeek.

### 16.1 Required human inputs before launch

- Exact sequence: `TGGQNleALT` versus `TGGFQNleALT`.
- Reference peptide identity.
- Construct termini and linker representation.
- Available IP/FP values and replicate information, if the report will mention
  experimental agreement.

If sequence or construct identity is unresolved, remain in `waiting_human`.

### 16.2 Stages and gates

1. **Backup topology**
   - Copy and checksum the original topology and link files.
   - Gate: backups exist and checksums are recorded.

2. **Patch NLE**
   - Worker writes the additive NLE block and required link lines.
   - Gate: a Met control before and after the patch is unchanged.

3. **Build and validate TGGFQNleALT**
   - Build from the validated parent structure.
   - Gate: no missing parameters, charge 0, Q5–Nle6 and Nle6–A7 bonds present,
     peptide C–N near 1.33 Å, and side-chain geometry within the runbook limits.

4. **Paired docking**
   - Rerun the Met parent as control and dock the Nle variant in the same batch.
   - Use the same receptor, ambig restraints, config, and sampling.
   - Current runbook parameters: s10000, 16 cores, 32 GB, 72 hours, cpucourt.
   - Gate: all required run records are terminal and required outputs exist.

5. **Analysis**
   - Recompute AIR-free with `w_air` read from each run config.
   - Require deep-groove analysis: core-19 at least 80 percent and zero NBD.
   - Require true clustfcc cluster size at least 50/200 for the main comparison.
   - Inspect the position-6 contact map and binding mode before comparing scores.
   - Nle improvement greater than 2–3 points in the same mode may motivate an
     extension. A difference within noise points to MD. Different modes cannot
     be compared directly.

6. **Final report**
   - Report computational evidence and limitations separately.
   - Do not claim experimental success from docking alone.
   - If laboratory numbers are absent, mark experimental validation as missing.

The optional reviewer should specifically check for cross-mode score comparison,
incorrect `w_air`, overclaiming, and missed position-6 anomalies.

## 17. Implementation phases and exit criteria

### Phase 0: v2 interface probes

Implement no campaign logic yet. Verify and record:

- `run --server` behavior and the v2 JSON event shape;
- `--auto` with explicit deny rules;
- every configured model route, including DeepSeek-V4.1-Flash and GLM-5.3;
- DeepSeek empty-output behavior when the token budget is too small;
- session ID capture and `session_message` usage fields;
- process-group termination;
- behavior after daemon and service restart.

Exit: small fixtures and probe notes exist. The parser and accounting design are
based on observed v2 output, not assumptions.

### Phase 1: schema and state machine

Implement protocol validation, atomic state, journal, locks, transitions, and
reconciliation with stub stage runners.

Exit: all schema, transition, lock, and restart unit tests pass without LLM or
network access.

### Phase 2: worker runner

Implement fresh opencode sessions, logs, output contracts, timeout, and retry.
Add the optional reviewer behind `reviewer.enabled` and `stage.review`.

Exit: stubbed worker and reviewer tests pass, plus a trivial live v2 smoke test.

### Phase 3: jobs and deterministic gates

Implement command templates, preflight checks, job mapping, waiting, reconcile,
cancel, and usage accounting.

Exit: fake job tests prove no duplicate submission and correct terminal-state
handling. Nle gate fixtures pass and fail correctly.

### Phase 4: supervised Nle pilot

Run the actual campaign only after human approval of the protocol and inputs.
The first run remains supervised even though the supervisor executes the steps.

Exit: all acceptance criteria in section 19 are met.

### Phase 5: optional follow-ups

Enable only pre-approved, bounded follow-up types. Keep new research directions
behind human approval.

Exit: triage tests prove scope, cost, and count enforcement. No arbitrary new
stage can be created by a worker or reviewer.

## 18. Test plan

### 18.1 Unit tests: schema and state

- Accept a valid protocol and reject unknown fields or stage types.
- Reject a missing model, gate, command, or required output.
- Hash the frozen protocol and detect later changes.
- Enforce every allowed transition and reject illegal transitions.
- Require final human approval when configured.
- Recover state from journal and receipts after interrupted writes.
- Prevent two campaign instances from holding the same lock.

### 18.2 Unit tests: workers

- Assert a fresh session is used for each new stage attempt.
- Assert explicit model, agent, server, database, cwd, and environment.
- Assert no previous lane session ID is reused.
- Parse valid worker output and reject malformed output.
- Retry only output-format failures up to the limit.
- Kill the complete process group on timeout.
- Preserve stdout, stderr, session ID, and usage on failure.

### 18.3 Unit tests: reviewer

- Run only when enabled for the stage.
- Give raw artifacts before the worker report.
- Reject review text without evidence paths.
- Route blocker, major, and minor issues correctly.
- Prove review text alone cannot advance a stage.
- Prove a reviewer cannot submit a job or modify the protocol.
- Record follow-ups without executing them when follow-ups are disabled.

### 18.4 Unit tests: jobs and restart

- Record run IDs before entering `waiting_job`.
- Do not resubmit when a recorded run exists.
- Reconcile terminal success, science failure, and infrastructure failure.
- Retry infrastructure failure once with identical command hashes.
- Refuse a second automatic retry.
- Cancel every active run on operator stop.

### 18.5 Unit tests: security

- Reject shell-string commands and non-approved argv.
- Reject a changed command hash at execution time.
- Reject worker writes outside `work/`.
- Reject protocol or approval changes from a worker or reviewer.
- Treat retrieved text as data, not a new stage definition.
- Verify secret redaction on test fixtures.

### 18.6 Nle gate fixtures

Create small fixtures for:

- valid NLE topology and links;
- missing NLE block or link line;
- changed Met control output;
- missing parameters or wrong charge;
- missing Q5–Nle6 or Nle6–A7 bond;
- incorrect `w_air` or different `w_air` across protocols;
- high AIR penalty and off-mode poses;
- cluster below and above the convergence threshold;
- position-6 contact differences;
- a report that overclaims experimental validation.

Each fixture must assert both the failing case and the nearest passing case.

### 18.7 Integration tests without live models

Use stub workers and a fake job backend to run:

- a full successful campaign;
- a gate failure followed by one repair;
- a reviewer blocker followed by human approval;
- a daemon restart while jobs are pending;
- an infrastructure retry;
- a hard budget stop;
- an operator stop with active jobs.

Assert final state, event order, receipts, job counts, and output hashes.

### 18.8 Limited live tests

Keep live model and cluster tests out of the default suite.

- One trivial v2 response through the dedicated service.
- One structured reviewer response for each configured model route, with an
  explicit timeout and empty-output check.
- One usage-accounting read from the real v2 database.
- One process-kill test with a harmless long-running command.
- One known-answer job through the normal `submit_job` path.
- One supervised Nle pilot after all previous gates pass.

### 18.9 Reviewer and automation evaluation

For the same completed stage artifacts, compare:

- no reviewer;
- DeepSeek-V4.1-Flash reviewer;
- GLM-5.3 reviewer;
- manual interactive review.

Record issues found, false positives, false negatives, token use, and human
minutes. This is an ablation, not a demonstration that more agents are better.

## 19. Pilot acceptance criteria

Accept the partial-automation pilot only if all are true:

1. After protocol approval, no routine prompt is needed before the final gate.
2. The supervisor waits for tracked jobs and starts analysis automatically.
3. Restarting the daemon during docking creates no duplicate jobs.
4. One induced infrastructure failure follows the approved retry policy.
5. Scientific ambiguity enters `waiting_human` instead of being guessed away.
6. Every reported metric is recomputed and linked to a source artifact.
7. The final report separates computational and experimental validation.
8. Operator stop terminates workers and cancels active jobs.
9. Token, core-hour, and human-time usage is present in the final record.
10. A live interactive hsc70 session can inspect the same project safely.

## 20. Explicit handoff instructions for the implementing agent

- Read this plan and the existing mailbox daemon before editing.
- Do not alter existing mailbox behavior except through reviewed shared helpers.
- Do not commit unless the user explicitly asks.
- Keep the default test suite free of live model and cluster calls.
- Use the v2 tree and pinned binary for implementation and probes.
- Run lint/type checks and the relevant pytest suites after implementation.
- Do not launch the real Nle campaign until the user approves the protocol and
  the unresolved construct questions are answered.
- If a design assumption conflicts with observed v2 behavior, stop and report
  the conflict instead of silently choosing another interface.

## 21. Implemented interfaces and operating commands

Code lives in `opencode_cc_mem/campaign_runner/`:

- `models.py`: protocol schema and model routing.
- `state.py`: campaign state and journal.
- `workers.py` and `process.py`: opencode and process execution.
- `reviewers.py`: independent review validation and routing.
- `gates.py`, `jobs.py`, `accounting.py`, and `security.py`: checks and records.
- `runner.py`: stage dispatch and recovery.
- `cli.py`: command line.

The v2 probe showed these JSON event types: `step_start`, `tool_use`,
`step_finish`, and `text`. Worker parsing uses those observed shapes.
Session usage is read from `session_message`; the standalone probe produced
zero rows in the `event` table but usable `session_message` rows.

Common commands:

```bash
magnolia-campaign validate campaigns/<name>/protocol.json
magnolia-campaign init campaigns/<name>/protocol.json \
  --run-dir projects/<project>/runs/<date>_<name> \
  --brief campaigns/<name>/brief.md
magnolia-campaign run <run-dir> --server http://127.0.0.1:<port>
magnolia-campaign daemon <run-dir> --server http://127.0.0.1:<port>
magnolia-campaign status <run-dir>
magnolia-campaign approve <run-dir> --approver <name> --reason <reason>
magnolia-campaign halt <run-dir> --reason <reason>
```

`halt` cancels recorded jobs before setting the halted state. A `HALT` file has
the same effect on the next supervisor pass. A `PAUSE` file pauses only at a
safe state boundary.

## 22. Main references

Local implementation references:

- Existing runner: `softwares/bin/magnolia-agents-daemon`.
- Existing design notes: `docs/agents-daemon-design.md` and
  `docs/daemon-agents-plan.md`.
- V2 capture: `.opencode/plugins/magnolia-session-capture.ts`.
- V2 accounting source: `compchem_memory/opencode_ingest.py` around
  `v2_db_exporter`.
- Campaign discipline: `.opencode/skills/campaign-protocol/SKILL.md`.
- Job checks: `rules/prejob_check.md` and `rules/job_execution.md`.

Evidence guiding the design:

- Anthropic ART: staged work, follow-up tasks, scripted checks, LLM supervisors,
  kill-tests, and raw-context sensitivity.
- Anthropic binder design: frozen protocol, calibrated ranking, and blind
  experimental validation.
- OpenAI distributed biological reasoning: parallel analysis, held-out checks,
  and blinded expert evaluation.
- OpenAI mathematics: proof generation with partial Lean formalization and
  explicit verification caveats.
