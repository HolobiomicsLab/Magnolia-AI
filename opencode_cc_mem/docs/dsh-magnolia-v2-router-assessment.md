# dsh-superagent and Magnolia v2: architecture assessment and build/test plan

Status: assessment for review, not approval to implement.
Date: 2026-11-02.
Branch: `exp/opencode-v2`.
Magnolia commit assessed: `80c61e5b4efc1c34a056a1fb53973c1f8241c119`.
dsh-superagent commit assessed: `6337fc823ce20251c8de8085948165c81aaa701e`.
Audience: the human designer and the coding agent that may implement a later slice.

## 1. Purpose

This document answers four questions:

1. Which parts of DeepSeek's dsh-superagent are useful for Magnolia v2?
2. Can a Router agent correctly assign work across projects?
3. How should operational state, learning memory, forum messages, and KGMemory interact?
4. What is the smallest implementation that can test the design without enabling uncontrolled autonomy?

The main conclusion is:

> Add a deterministic event and Work Item control plane around the existing campaign supervisor. Keep project-scoped learning memory. Add KGMemory first as a provenance and retrieval projection, not as the live work-state system.

Do not replace the campaign supervisor. Do not import dsh's `danger-full-access` execution model. Do not treat dsh's scheduling results as evidence of correct routing or correct scientific work.

## 2. Evidence and limits

### 2.1 Sources reviewed

DeepSeek sources:

- The design essay, "Super-agent 设计探索：迈向更自主的 Agent 系统", published 2026-10-31.
- The public repository README and `docs/000` through `docs/007` at the commit named above.
- The public agent-preset configuration and architecture description.

Magnolia sources:

- `campaign_runner/`, including state, runner, workers, reviewers, jobs, gates, and distillation.
- `softwares/bin/magnolia-agents-daemon` and the doorbell plugin.
- The compchem-memory server and project-tier run records.
- The campaign build and remediation documents.
- The xiulian v2 interface and service probes dated 2026-10-24.

Two independent static reviews checked the comparison. No files were changed during that review.

### 2.2 Evidence limits

- Neither test suite was rerun for this assessment.
- DeepSeek's performance numbers are vendor-reported.
- DeepSeek's public prototype differs from its internal system and from parts of the essay.
- Magnolia's reported live worker and local-job smokes are documentary evidence, not results reproduced here.
- Magnolia still lacks documented live validation of a real Slurm campaign job and a real independent reviewer model. No real Nle protocol exists.
- Architectural recommendations below are proposals, not experimentally established findings.

## 3. Executive assessment

### 3.1 What dsh does well

dsh provides a strong reference for the event control plane:

- immutable Triggers;
- explicit Threads;
- deterministic admission before model judgment;
- scoped Mandates with Policy versions;
- Tickets separate from Assignments;
- Subscriptions that deliver context without granting authority;
- durable effects and outbox receipts;
- structured recovery and human-attention states.

### 3.2 What Magnolia does well

Magnolia provides stronger support for scientific execution and learning:

- approved protocols;
- deterministic stage gates;
- independent read-only reviewers;
- durable run records and job lifecycle controls;
- token, cost, core-hour, wall-time, and attempt budgets;
- session evidence, staging entries, confirmed project entries, and reviewed rules;
- explicit human approval for ambiguous science and final claims.

These are complementary strengths.

### 3.3 The main Magnolia gap

Magnolia has three connected subsystems but no shared Work Item model:

1. The mailbox treats a letter as the work.
2. The campaign runner treats a campaign, stage, and attempt as the work.
3. Memory treats a session, entry, or rule as the knowledge record.

The memory system itself is not weak. The gap is the missing authoritative chain:

```text
request -> authorization -> execution -> evidence -> final outcome -> lesson
```

### 3.4 The Router answer

A Router agent can help assign work, but an LLM must not be the whole Router.

The LLM may classify, attach, or propose. Deterministic code must own:

- identity and deduplication;
- sender and project authorization;
- Mandates and scope;
- Work Item state;
- campaign and job submission;
- delivery and recovery;
- cross-project write boundaries.

dsh supplies useful engineering safeguards but no published routing-accuracy benchmark. Correct Router judgment remains something Magnolia must measure itself.

### 3.5 The KGMemory answer

KGMemory can improve provenance, project-scoped retrieval, and cross-project discovery. It does not replace the control plane.

A shared graph is acceptable if project isolation is logical and enforced. A project node alone is not enough. The graph also needs Work Items and evidence links.

## 4. What dsh establishes

### 4.1 Implemented control-plane model

The public prototype describes and implements this flow:

```text
Source -> Trigger -> Thread -> Eval -> Ticket + Mandate -> Assignment -> Worker
                     |
                     -> Subscription -> existing Ticket
```

Important properties from `docs/001-domain.md`, `docs/002-authorization.md`, and `docs/007-architecture.md`:

- A Trigger is immutable and belongs to one or more Threads.
- Accepting the same Trigger twice creates one Ticket.
- The deterministic gate checks the sender and Source policy.
- The model assessment returns a Decision but does not own domain state.
- The Engine is the single writer of Triggers, Threads, Decisions, Tickets, and Assignments.
- A Mandate records scope, strength, matched rules, sender, and Policy version.
- A Subscription delivers context but grants no new authority.
- A Ticket owns its lifecycle independently of its Executor Session.
- Stored work is committed before external side effects run.

The domain core is deterministic. The complete routing system is not, because the main LLM Session still judges candidates.

### 4.2 What the experiments show

DeepSeek's essay reports these preliminary results:

- About 300 messages and about 20 work requests per end-to-end experiment.
- Five runs per configuration.
- Super-agent p50 and p95 completion times of 12.4 and 15.7 minutes.
- A neutral single-session baseline p95 of 29.5 minutes.
- Removing direct subscription delivery increased read-cache tokens from 23.23M to 31.77M.
- In the batching test, increasing arrivals from about 1 to 40 Triggers per minute changed average batch size from 1.0 to 2.8, request time from 2.5 to 3.9 seconds, and amortized decision time from 6.4 to 1.6 seconds.

The essay also states that coding was replaced by a time-consuming tool. The experiments measured scheduling and coordination, not task execution quality.

The defensible conclusion is:

> dsh provides preliminary latency, throughput, and token-cost evidence under synthetic load. It does not establish routing accuracy, artifact correctness, or scientific reliability.

No published numbers were found for false acceptance, missed requests, wrong-Ticket attachment, unauthorized resume, or delivered-task correctness.

### 4.3 What remains open or absent in the public prototype

The essay itself identifies these open problems:

- cross-month long-term memory in section 8.1;
- human collaboration after Session abstraction in section 8.2;
- isolated-instance A2A communication in section 8.3;
- security and privacy in section 8.4.

The public prototype has substantial operational persistence: SQLite, Harness Sessions, durable effects, outbox receipts, and restart recovery. It does not expose an equivalent to Magnolia's learning-memory lifecycle:

```text
session evidence -> staging -> confirmed project entry -> reviewed rule
```

The public deployment is a single-host Harness profile. It still uses external services. This is not the essay's complete cloud-AgentLoop and endpoint-daemon separation.

The deployment guide includes an hourly external health check. That is useful, but it is not the full essay Watchdog that judges substantive worker progress and changes prompts or trigger rules.

### 4.4 Security posture

`docs/006-deployment.md` states that both managed presets run with `danger-full-access` as the operator's OS user. It also states that account wrappers and Ticket directories are not a security boundary.

A worker can execute programs by absolute path and read the operator's home directory. DeepSeek recommends a dedicated OS account for isolation.

Magnolia should follow the isolation principle, not copy the full-access default.

## 5. Magnolia v2 baseline

### 5.1 Mailbox daemon

The mailbox daemon is a lightweight activator:

- It discovers task letters and scheduled letters.
- It processes tasks sequentially.
- It resumes one persistent opencode session per project lane across letters.
- It checks for a `FINAL ANSWER` marker, not scientific correctness.
- It marks letters `done` or `error` and records a content hash.
- It supports a file-based `wait_for: job:<run_id>` mechanism.

Relevant code:

- Persistent lane sessions: `softwares/bin/magnolia-agents-daemon:277-300`.
- Completion check: `softwares/bin/magnolia-agents-daemon:301-316`.
- Reply and status writes: `softwares/bin/magnolia-agents-daemon:319-358`.
- Sequential scan: `softwares/bin/magnolia-agents-daemon:362-371`.
- `wait_for`: `softwares/bin/magnolia-agents-daemon:374-415`.

This is not an evaluated Ticket-to-worker control plane.

### 5.2 Campaign supervisor

The campaign supervisor is the strongest execution component:

- The supervisor contains no LLM.
- Workers produce artifacts but cannot certify completion.
- Reviewers criticize but cannot approve.
- Deterministic gates advance stages.
- State transitions use legal-state checks, a journal, locks, and compare-and-swap checks.
- Campaign workers start fresh for each stage attempt, except output-format retries.
- Jobs use durable records and duplicate-submission protection.
- Human stages handle approval and scientific ambiguity.

Relevant code:

- State transitions: `campaign_runner/state.py:460-527`.
- Validation and advancement: `campaign_runner/runner.py:706-820`.
- Worker session behavior: `campaign_runner/workers.py:165-254`.
- Reviewer behavior: `campaign_runner/reviewers.py`.

The gate framework does not guarantee scientific correctness by itself. It is only as good as its configured checks and oracle.

### 5.3 Learning memory

Magnolia already separates:

- exact session evidence;
- provisional staging entries;
- confirmed project entries;
- git-reviewed rules and on-demand skills.

The campaign plan gives the correct authority rule: project memory is guidance, never authority over the protocol. See `docs/campaign-supervisor-build-plan.md:614-629`.

Memory should not become a work queue. It should receive evidence from an authoritative Work Item lifecycle.

### 5.4 Evidence maturity

The remediation document reports 219 passing tests and successful local worker and job smokes. Those results were not rerun here.

The same document lists remaining live validation for a real Slurm job and a real independent reviewer. It also states that no Nle protocol has been created. See `docs/campaign-supervisor-remediation-plan.md:3-14`.

The campaign integration tests use fake workers and fake job backends. They demonstrate intended behavior, not live model or cluster correctness.

## 6. Side-by-side assessment

| Area | dsh-superagent | Magnolia v2 | Assessment |
|---|---|---|---|
| Purpose | Proactive coding for one GitHub repository, optionally Feishu | Multi-project scientific agents, campaigns, jobs, and learning | Different scopes |
| Event intake | Source, Trigger, Thread, Eval | Letters, schedules, job notices, campaign CLI | dsh is more unified |
| Authorization | Deterministic gate, Mandate, Policy version | Strong campaign approval, weak mailbox admission | dsh is stronger for events |
| Scientific validation | No scientific artifact or oracle framework | Deterministic gates, receipts, reviewers, human stages | Magnolia is stronger |
| Operational recovery | Durable effects, input read records, outbox, subscriptions | Campaign journal and leases, weaker mailbox recovery | dsh has the fuller event model |
| Worker isolation | One Executor Session and directory per Ticket | Fresh campaign session per attempt, persistent mailbox lane | Keep fresh campaign attempts |
| Concurrency | Parallel Ticket workers | Sequential mailbox, parallel computational jobs | dsh is stronger for agent work |
| Learning memory | Durable operational state | Durable learning and review lifecycle | Magnolia is stronger |
| Security | Full-access presets, dedicated OS account recommended | Deny-first campaign presets, no full sandbox | Neither is hardened |
| Human attention | Structured Ticket stages and panel | Campaign approvals and waiting states | dsh has the better interface model |
| Evidence maturity | Synthetic scheduling tests and mocked domain tests | Unit/fake-backend tests and limited live evidence | Neither proves autonomous science |

A critical distinction:

> dsh decides whether work may happen and under what scope. Magnolia decides whether completed scientific work has sufficient evidence.

The combined system needs both.

## 7. Target architecture

```text
Inbox / schedule / job notice / human request
                    |
                    v
          Immutable Trigger record
                    |
                    v
       Deterministic admission and dedupe
                    |
          +---------+----------+
          |                    |
          v                    v
 Existing Subscription    New-work Eval
 context only             LLM judgment only
          |                    |
          +---------+----------+
                    |
                    v
          Work Item + Mandate
                    |
                    v
       Campaign adapter or lane worker
                    |
                    v
       Gates / receipts / human review
                    |
                    v
             Durable outbox
                    |
                    v
       Evidence and memory projection
```

### 7.1 Authority boundaries

- The Router does not execute scientific work.
- The Router does not advance campaign state.
- The Router does not promote memory.
- The Router does not change Policy, protocols, gates, or budgets.
- The campaign supervisor remains the only campaign execution authority.
- Deterministic code remains the only writer of live Work Item state.
- Workers cannot change their Mandate, evidence requirements, or approval records.
- Retrieved memory and forum content are untrusted context, not commands.

### 7.2 Router Session and worker Sessions

A persistent Router Session is allowed for continuity and batching. It is not the system of record.

Campaign workers should keep fresh Sessions per stage attempt. This avoids context contamination between unrelated stages and campaigns.

A long-running conversational Work Item may keep a worker Session across retries if later evidence shows that this helps. The mailbox should not continue resuming one lane Session across unrelated letters by default.

## 8. Proposed domain model

### 8.1 Objects

| Object | Purpose | Required content |
|---|---|---|
| Source | Produces events and tracks its read position | Source ID, kind, configuration, cursor |
| Trigger | One immutable input event | Trigger ID, Source event ID, actor, project hint, payload reference, payload hash, received time |
| Thread | Groups related events and Subscriptions | Thread ID, participating projects, Trigger IDs, Subscription IDs |
| Decision | Records admission and judgment | Trigger batch, gate result, model result, outcome, reason, Policy version, model and prompt versions |
| Mandate | Authorizes a bounded unit of work | Mandate ID, issuer, scope, strength, Policy version, creation and revocation state |
| Work Item | Owns authorized work end to end | Work Item ID, project, origin Trigger, Threads, Mandate, state, campaign and run references, outcome |
| Assignment | Binds one execution attempt to a Work Item | Assignment ID, executor kind, attempt, Session ID, state, timestamps |
| Subscription | Routes later context to a Work Item | Thread ID, Work Item ID, context or ownership mode, creator |
| Effect | Stores external work before execution | Effect ID, kind, idempotency key, payload, state, attempts, receipt |
| EvidenceRef | Connects work to exact evidence | Evidence kind, run or artifact ID, path, hash |
| MemoryLink | Connects a lesson to its origin | Work Item ID, memory entry ID, relation, evidence references |

### 8.2 Work Item states

Recommended initial states:

```text
candidate -> authorized -> queued -> running -> waiting_human
          -> ignored
authorized -> stopped
queued/running/waiting_human -> stopped
running -> completed | failed | waiting_human
waiting_human -> queued | stopped
```

The exact transition table must be code, not prompt text.

Assignment states remain separate:

```text
pending -> running -> ended
```

A Work Item does not infer its state from free-form worker narration. It uses structured reports, deterministic gates, and human decisions.

### 8.3 Required invariants

1. `(source_id, source_event_id)` is unique.
2. Accepting one Trigger twice creates one Work Item.
3. Only an admitted Decision can create a Mandate.
4. Only the control engine writes live Work Item state.
5. Only the campaign supervisor advances campaign state.
6. A Subscription delivers context, never authority.
7. A wider scope requires a new Mandate.
8. A person's stop prevents automatic resume.
9. Workers cannot edit Policy, Mandates, gates, or approval records.
10. External effects are stored before execution and have idempotency keys.
11. Raw evidence remains available even if a memory claim is revised.
12. Project memory never authorizes work.
13. Cross-project memory writes are denied by default.
14. A completed Work Item must link to its final evidence and outcome.

## 9. Router judgment and admission

### 9.1 Two-stage Eval

Use the dsh fast-path and slow-path distinction:

1. **Deterministic gate:** Source enabled, sender allowed, schema valid, project allowed, duplicate check, budget check, explicit-stop check.
2. **Model assessment:** accept, attach, stop, or ignore, using bounded Thread and Work Item context.

The model cannot override a failed deterministic gate.

### 9.2 Accept modes

Implement three modes from the start:

- `off`: no inferred work is accepted.
- `observe`: inferred Decisions are recorded but not executed.
- `auto`: an inferred Decision may create authorized work within its enabled rules.

Explicit human requests can work in every mode, subject to the gate.

### 9.3 Attachment rules

Do not attach work by topic similarity alone.

Initial attachment should require at least one explicit reference:

- a Work Item ID;
- a campaign or run ID;
- a Thread ID;
- a reply to a known message;
- an unambiguous artifact reference.

KGMemory may propose related Work Items. The deterministic gate and evaluated attachment rule must still decide whether the proposal is usable.

### 9.4 Follow-up ownership

A Work Item that owns a human follow-up either:

- handles it within the existing Mandate; or
- hands it back with a structured reason.

Only an unowned follow-up returns to Eval. This prevents two agents from answering the same request.

## 10. Forum model

The proposed forum is a routing and evidence layer, not a new memory tier.

### 10.1 Rules

- Every post belongs to a Thread.
- Every post has an immutable ID and author identity.
- Threads may have many Work Item subscribers.
- Subscriptions may be owning or context-only.
- Context-only posts never expand a Mandate.
- A human stop is authoritative.
- Replies use durable outbox records.
- Unread owned follow-ups return to Eval when a Work Item ends.
- Cross-project posts carry source project and provenance.

### 10.2 Cross-project knowledge transfer

Do not copy project memory bodies into a shared Router context.

Transfer explicit artifacts instead:

- an idea ticket;
- a digest;
- a gate report;
- a run record;
- a reviewed lesson proposal.

The receiving project decides whether to distill or confirm the artifact through its normal path.

## 11. Memory and KGMemory

### 11.1 Keep three kinds of record distinct

1. **Operational state:** Triggers, Threads, Work Items, Mandates, Assignments, Effects.
2. **Exact evidence:** transcripts, run records, artifacts, receipts, approvals.
3. **Learning memory:** sessions, staging entries, project entries, and rules.

Connect them through stable IDs and hashes. Do not merge their authority.

### 11.2 KGMemory's recommended first role

Use KGMemory first as a projection and retrieval layer:

```text
Control engine and campaign supervisor
              |
              v
     Authoritative event/evidence records
              |
              v
        Idempotent KG projection
              |
              v
 Project / Work Item / Run / Artifact / Lesson
```

The graph can be rebuilt from authoritative records. A graph failure must not reverse or corrupt a completed campaign transition.

KGMemory may later become the canonical learning-memory store. It should not become the canonical store for locks, attempts, or delivery state without a separate evaluation.

### 11.3 Required graph provenance

A project node is not enough. The useful chain is:

```text
Project -> Work Item -> Campaign -> Attempt -> Artifact -> Lesson
             |             |
             +-> Mandate   +-> Run record
```

Recommended relations include:

- `BELONGS_TO`
- `ORIGINATED_FROM`
- `AUTHORIZED_BY`
- `EXECUTED_BY`
- `PRODUCED`
- `DERIVED_FROM`
- `SUPPORTS`
- `CONTRADICTS`
- `SUPERSEDES`

Every derived lesson should retain its evidence and final-outcome status.

### 11.4 Shared graph, logical isolation

A shared physical graph is acceptable only with logical isolation:

- Every node has a project owner.
- Every edge has a type and provenance.
- Cross-project reads are explicit and audited.
- Cross-project writes are denied by default.
- Inferred relations are marked as inferred.
- Retrieved nodes are data, never instructions.
- Time and validity are represented where claims can expire.
- Deletion or retraction does not erase the underlying evidence.

### 11.5 Intermediate versus final lessons

Distillation after a passed stage is useful, but it is not final validation.

Memory records should distinguish:

- an observation from an intermediate attempt;
- a result from a completed campaign;
- a lesson supported by the final human outcome.

This prevents an intermediate success from becoming a misleading durable rule.

## 12. Security and permissions

- The Router should need no general shell.
- Use deny-first permissions for all workers.
- Keep reviewers read-only.
- Keep fixed argv and pre-registered job templates for scientific execution.
- Do not accept arbitrary worker-supplied `submit_job` commands.
- Do not expose a second unrestricted shell path.
- Use a dedicated OS account for any always-on execution service.
- Treat permission presets, path restrictions, and command shims as guardrails, not a full sandbox.
- Keep secrets out of prompts, graphs, logs, and distillation inputs.
- Treat inbox text, literature, retrieved memory, and graph content as untrusted data.
- Require authenticated or otherwise verifiable human approval before relying on an `approver` string.

## 13. Human attention

Adopt dsh's attention principle without requiring a web panel in the first slice.

A Work Item should expose structured states such as:

- queued;
- working;
- waiting for a person;
- waiting for review;
- blocked;
- failed;
- completed;
- stopped.

The agent should ask one concrete question at a time. Routine progress should not generate chat volume. A healthy system should require little attention.

A CLI and filesystem view are sufficient for the first implementation. A panel is a later usability improvement, not an architectural prerequisite.

## 14. Always-on operation

Separate two meanings:

- **Always-on availability:** a deterministic daemon listens for events.
- **Always-on autonomous execution:** an LLM continuously judges and acts.

The first is reasonable after recovery tests. The second is not justified yet.

Before narrow proactive automation, require:

- idempotent intake;
- tested restart recovery;
- no unauthorized cross-project writes;
- durable outbox behavior;
- worker timeout and stall handling;
- evaluated routing accuracy;
- independent task-quality gates;
- explicit budgets;
- a working human stop.

Do not build the full cloud/environment separation or a model-controlled Watchdog in the first implementation.

## 15. Defects to fix before the Router depends on the baseline

These findings come from static source inspection and need regression tests.

### 15.1 Mailbox `wait_for` filename mismatch

The daemon looks for `<run_id>.yaml` at `softwares/bin/magnolia-agents-daemon:397`.

The memory layer writes `<YYYYMMDD>_<run_id>.yaml` at `mcp-servers/compchem-memory/src/compchem_memory/tiers/project.py:616-619`.

Normal run records can therefore remain unseen.

### 15.2 `wait_for` releases on non-terminal status

`softwares/bin/magnolia-agents-daemon:401-408` accepts any status string. A `submitted` or `running` record can release the letter.

Require an explicit terminal-state set.

### 15.3 Campaign memory-state check is not called in production

`verify_memory_state()` exists at `campaign_runner/state.py:399-404`. Only its test calls it in this checkout.

Decide whether memory changes should block, warn, or be recorded. Enforce that decision in the runner.

### 15.4 Cross-project write guards are incomplete

`memory_record_learning` uses the project guard at `compchem_memory/server.py:411-414`.

`memory_confirm` and `memory_apply_promotions` accept another `project_dir` without the same guard at `compchem_memory/server.py:640-649` and `:1008-1026`.

All write tools need a consistent policy before agents route across projects.

### 15.5 Mailbox and inbox lifecycles differ

The inbox rule describes replies in the sender's inbox and `open/answered/closed` states. The daemon writes sibling replies and `done/error` states.

Reconcile these models while preserving the documented legacy-thread exception.

## 16. Implementation sequence

Each phase has an exit condition. Do not start the next phase before the previous one passes.

### Phase 0: repair and pin the baseline

Work:

- Fix the defects in section 15.
- Add regression tests.
- Rerun the campaign and memory suites.
- Record which live validations remain missing.

Exit condition:

- Baseline tests pass.
- Cross-project write behavior is explicit and tested.
- Terminal job waiting works with production run-record names.

### Phase 1: deterministic event and Work Item core

Work:

- Add Source, Trigger, Thread, Decision, Mandate, Work Item, Assignment, Subscription, and Effect records.
- Add one transactional control store with one writer.
- Implement idempotent intake and explicit human acceptance.
- Implement Work Item and Assignment transition tables.
- Add no LLM Eval and no proactive execution.

Implementation recommendation: use a small SQLite control store per tree. SQLite is an implementation choice, not a requirement of the architecture. KGMemory is not this store.

Exit condition:

- Duplicate input creates no duplicate Work Item.
- Illegal transitions fail.
- Restart preserves all accepted work and pending effects.

### Phase 2: campaign adapter vertical slice

Work:

- Connect an accepted Work Item to a pre-registered campaign protocol.
- Preserve campaign startup approval, pre-job checks, gates, and receipts.
- Return campaign and run references to the Work Item.
- Use a fake worker and fake backend first.

Exit condition:

- One Work Item produces one campaign with the expected protocol hash.
- A retry cannot create a duplicate campaign or job.
- Campaign outcomes map back to the correct Work Item.

### Phase 3: Subscriptions, hand-back, and outbox

Work:

- Add owning and context-only Subscriptions.
- Add follow-up ownership and hand-back.
- Add durable replies with receipts.
- Add human stop and resume rules.

Exit condition:

- A follow-up reaches exactly one owner or returns to Eval.
- Context-only messages do not expand scope.
- Restart does not duplicate or lose replies.

### Phase 4: LLM Eval in observe mode

Work:

- Add the bounded assessment prompt and structured Decision schema.
- Record model, prompt, and Policy versions.
- Replay labeled historical and synthetic events.
- Do not execute inferred accepts.

Exit condition:

- The Decision schema is valid on every replay.
- Routing and attachment metrics meet pre-registered thresholds.
- No failed deterministic gate is overridden by the model.

### Phase 5: limited live routing

Work:

- Enable explicit requests through the campaign adapter.
- Keep inferred acceptance in observe mode.
- Measure tokens, latency, human minutes, and task-gate outcomes.

Exit condition:

- The live pilot passes its independent task-quality gate.
- Human stop and recovery work under supervision.
- No unauthorized campaign, job, or memory write occurs.

### Phase 6: KGMemory projection

Work:

- Project Projects, Work Items, campaigns, runs, artifacts, and lessons into KGMemory.
- Make projection idempotent and rebuildable.
- Add project-scoped retrieval and provenance queries.
- Test cross-project read and write boundaries.

Exit condition:

- A lesson can be traced to its exact Work Item and evidence.
- Graph rebuild produces the same projection.
- Cross-project access tests pass.
- Graph failure does not affect campaign state.

### Phase 7: narrow proactive automation

Work:

- Select one low-risk work class.
- Enable `auto` only for that class.
- Keep every other class in `observe` or `off`.

Exit condition:

- Pre-registered live safety, quality, cost, and attention thresholds hold.
- Automatic stop and rollback work.
- The human approves keeping the automation enabled.

## 17. Suggested code layout

This is an implementation suggestion, not a required file layout:

```text
router_runner/
  models.py
  store.py
  engine.py
  admission.py
  evaluator.py
  subscriptions.py
  effects.py
  campaign_adapter.py
  memory_projection.py
  kg_projection.py
  cli.py
```

Prefer a new package over growing either `magnolia-agents-daemon` or `campaign_runner` into a second control framework.

The campaign adapter should expose a narrow interface. It should not give the Router arbitrary protocol or shell execution.

## 18. Evaluation plan

### 18.1 Deterministic unit tests

Cover:

- unique Trigger intake;
- duplicate accept;
- legal and illegal transitions;
- Mandate scope and revocation;
- Policy version changes;
- Subscription context versus authority;
- follow-up ownership and hand-back;
- human stop and resume;
- durable effect retries;
- outbox ordering;
- campaign idempotency;
- cross-project read and write rules.

### 18.2 Router replay corpus

Build a frozen corpus from historical letters and synthetic edge cases.

Each case should contain:

- Source event and actor;
- Thread context;
- existing Work Items;
- current Policy;
- expected gate result;
- expected accept, attach, stop, or ignore outcome;
- expected Work Item, if any;
- expected authority and scope.

Include adversarial cases:

- duplicated events;
- two similarly named projects;
- two similar open Work Items;
- prompt-injection text in a letter;
- a follow-up after a human stop;
- a message arriving during restart;
- a stale Policy version;
- a graph suggestion pointing to the wrong project.

Measure:

- false acceptance;
- missed explicit requests;
- wrong-Work-Item attachment;
- duplicate Work Items;
- lost follow-ups;
- unauthorized resume;
- Decision schema validity;
- token use and latency.

### 18.3 Fault-injection tests

Interrupt the system:

- after Decision commit but before Work Item creation;
- after Work Item creation but before campaign creation;
- after campaign creation but before job submission;
- after external submission but before recording the result;
- during reply delivery;
- during memory or graph projection.

The expected result must be explicit for every interruption. Unknown submission outcomes must go to human review rather than automatic duplicate submission.

### 18.4 Security tests

Verify:

- forum text cannot change Policy;
- graph text cannot change Mandates;
- a worker cannot write control state;
- a reviewer cannot approve;
- a project cannot write another project's memory;
- arbitrary shell and job commands are refused;
- secrets do not appear in logs or distillation inputs;
- a stopped Work Item cannot resume automatically.

### 18.5 KGMemory tests

Verify:

- projection idempotency;
- full rebuild equivalence;
- project ownership on every node;
- typed and provenance-bearing edges;
- inferred versus confirmed relations;
- lesson-to-evidence traceability;
- contradiction and supersession history;
- cross-project read auditing;
- graph outage isolation from campaign execution.

### 18.6 Task-quality evaluation

Control-plane correctness is not enough.

Use known-answer tasks with an independent oracle. Compare:

- current mailbox flow;
- deterministic Router flow;
- Router plus LLM Eval;
- optional reviewer on and off.

Measure:

- oracle-scored quality;
- false-success declarations;
- false alarms;
- tokens and cost;
- wall time;
- human minutes;
- number of interventions.

Do not keep a reviewer or extra agent merely because it sounds useful.

## 19. Acceptance gates

Hard safety gates:

- Zero unauthorized campaign or job submissions.
- Zero unauthorized cross-project writes.
- Zero duplicate Work Items for the same Source event.
- Zero automatic resumes after a human stop.
- Every completed Work Item has an outcome and evidence chain.
- Every external side effect has a durable idempotency record.

Quality and efficiency gates should be pre-registered after the baseline replay. Do not invent thresholds before measuring the current system.

At minimum, compare:

- routing precision and missed-request rate;
- attachment accuracy;
- recovery success;
- task-oracle quality;
- human minutes per completed Work Item;
- tokens and cost per accepted Work Item.

## 20. First vertical slice

The first build should be deliberately small:

1. Use two projects: literature and xiulian.
2. Create one synthetic idea letter with an explicit human approval.
3. Ingest it as one Trigger.
4. Create one Work Item and one Mandate.
5. Route it through a fake campaign adapter.
6. Produce a deterministic receipt and final outcome.
7. Deliver one receipted reply.
8. Create the evidence links.
9. Restart between every two steps in the fault-injection variant.
10. Repeat with the KG projection enabled.

Do not include in the first slice:

- inferred acceptance;
- proactive sweeps;
- parallel production workers;
- real Slurm submission;
- automatic memory promotion;
- a web panel;
- cloud execution;
- a model-controlled Watchdog.

The first slice tests the architectural claim that matters most:

> One authorized event can become one traceable Work Item, one execution, one outcome, and one evidence-linked knowledge projection without duplication or loss of authority boundaries.

## 21. Non-goals for the initial implementation

- Replacing the campaign supervisor.
- Importing dsh code wholesale.
- A general autonomous research organization.
- Arbitrary task creation by the Router.
- A shared undifferentiated memory space.
- KGMemory as the live transaction and lock manager.
- Automatic merging, publishing, protocol modification, or memory promotion.
- Unrestricted worker shell access.
- Unattended runs longer than the existing campaign safety limit.

## 22. Decisions still needed before implementation

1. Confirm SQLite as the first control store, or select another transactional store.
2. Confirm the final Work Item state names and their mapping to existing inbox states.
3. Decide whether to preserve the legacy mailbox lifecycle behind an adapter or migrate it.
4. Define authenticated human approval rather than trusting an approver string.
5. Select the first pre-registered campaign protocol for the live pilot.
6. Specify KGMemory's actual API, transaction model, access-control support, and provenance fields.
7. Decide which memory changes should block a running campaign, and which should only be recorded.
8. Set quality and efficiency thresholds after baseline replay.

## 23. Final recommendation

Build the hybrid in small, testable steps:

> Use a dsh-inspired deterministic event and Work Item layer around Magnolia's campaign supervisor. Keep exact evidence and project-scoped learning memory. Project their relationships into KGMemory. Let the LLM advise on routing only after the deterministic authority and recovery paths work.

This preserves Magnolia's scientific controls while adding the routing, forum, recovery, and provenance mechanisms that dsh handles well.

## 24. Primary references

DeepSeek:

- Repository: https://github.com/deepseek-ai/dsh-superagent-prototype
- Commit assessed: https://github.com/deepseek-ai/dsh-superagent-prototype/commit/6337fc823ce20251c8de8085948165c81aaa701e
- Design essay: https://mp.weixin.qq.com/s/zoS7GlReWPhfCjENGxXCdg
- Domain model: `docs/001-domain.md` in that repository.
- Authorization: `docs/002-authorization.md`.
- Execution: `docs/003-execution.md`.
- Prompts: `docs/004-prompts.md`.
- Panel: `docs/005-panel.md`.
- Deployment: `docs/006-deployment.md`.
- Architecture: `docs/007-architecture.md`.

Magnolia:

- `docs/campaign-supervisor-build-plan.md`
- `docs/campaign-supervisor-remediation-plan.md`
- `docs/daemon-agents-plan.md`
- `docs/agents-daemon-design.md`
- `rules/inbox.md`
- `campaign_runner/`
- `softwares/bin/magnolia-agents-daemon`
- `mcp-servers/compchem-memory/src/compchem_memory/`
