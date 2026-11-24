# Always-on agent system (v0.1, PROVISIONAL)

Branch: `exp/always-on-agents`. The deliverable is the SYSTEM, not a challenge project.

All design choices are provisional and subject to change — see
`router-prototype-decisions.md` and `dsh-magnolia-v2-router-assessment.md`.

For the separate, opt-in conversation runtime that bridges durable Work Items
to the existing mailbox executor, see `always-on-runtime.md`. It does not change
this document's availability-versus-autonomy boundary or task-pack execution.

## What the system is

Three cooperating pieces, each with a distinct authority boundary:

```text
letters (JSON files in an inbox)
      |
      v
agentd  — deterministic intake + admission + approval staging (NO execution)
      |
      v
ControlEngine (router_runner) — the single writer of Work Item state:
      Trigger -> gate -> candidate -> operator approval -> Decision + Mandate
      -> queued -> assignment + durable Effect -> execution -> outcome
      |
      v
CompositeAdapter — fake protocols (control demos) OR real task packs:
      demo-taskpack-v0.1 -> DemoTaskPack; memory-dreaming-v0.1 -> DreamingTaskPack
      |
      v
receipt + evidence links (run record, gate report, ranked candidates)
```

The **campaign supervisor** remains the only campaign execution authority for
real scientific campaigns; **project memory** remains guidance, never
authority. agentd adds no new execution authority of its own.

## Availability vs autonomy (assessment section 14)

- **Always-on availability — implemented.** `agentd watch --inbox DIR`
  deterministically ingests letters and stages approval requests. It never
  executes anything, never calls an LLM, and is idempotent across sweeps.
- **Always-on autonomous execution — deliberately NOT implemented.** Execution
  requires an operator-terminal approval (single-use nonce + content hash) and
  an explicit `agentd execute`. No inferred acceptance, no proactive sweeps.

## Task packs are data, not core code

The always-on core is project-agnostic. A task pack is a manifest plus an adapter,
loaded from `router_runner/taskpacks/`; the core knows a pack only by its `id`,
`kind`, protocol hash and adapter. It never imports a project module or builds a
project path.

Current packs:

- `demo-taskpack-v0.1` — a generic pack with a real run/reconcile path, used by
  the control-plane and recovery tests.
- `memory-dreaming-v0.1` — an offline dreaming pass on a throwaway store.
- `nk2r-challenge-v0.1` — retired; its protocol hash is kept so old receipts stay
  readable, and execution is refused.

### Run the demo pack end to end

```sh
cd opencode_cc_mem
rm -f /tmp/agentd-demo.db
python3 -m router_runner.agentd demo-taskpack --db /tmp/agentd-demo.db
```

Expected: letter ingested -> approval -> demo task pack executes ->
`outcome=completed selected=3`, all acceptance items true, candidate CSV path,
receipted reply, `DEMO-TASKPACK OK`.

### Event-driven operation (`agentd run`)

Wakes are only useful if something is listening. `agentd run` is the
continuous loop (availability only — it never executes work):

```sh
cd opencode_cc_mem
python3 -m router_runner.agentd run \
    --project-dir projects/<project> --db <control.db> --interval 60
```

Each cycle: (1) ingest opening task letters from `<project>/inbox/*.json`,
(2) consume terminal job notices from `<project>/inbox/from-jobs/`, (3) bridge
every newly woken Work Item into `<project>/inbox/<wi>__<attempt>.task.md`.
The mailbox daemon (`magnolia-agents-daemon`) already scans `*.task.md` and
resumes the lane session, so the woken worker receives a self-contained letter
with the job outcome, the delta, the evidence index, and the expected
`FINAL ANSWER:` contract. The bridge is idempotent per Work Item+attempt.
Use `--once` for a single sweep (tests/cron).

### Full local tour (one command, no Slurm, no LLM)

```sh
cd opencode_cc_mem
rm -f /tmp/agentd-local.db
python3 -m router_runner.agentd demo-local --db /tmp/agentd-local.db \
    --workdir /tmp/agentd-local-demo
```

Eight narrated stages: letter -> approval -> park on a cluster job -> mid-wait
user follow-up -> the PRODUCTION `job_watcher.emit_job_notice` writes a real
notice -> wake-up with the job outcome AND the user message in the delta ->
batch 1 executes and its finding is promoted to `completed_campaign` -> a
follow-up opens a linked round-2 Work Item and executes. Prints artifact paths
(control DB, both candidate CSVs, the notice JSON) at the end.

Also `demo-jobloop` for the shorter park/wake variant, and `demo` in
`router_runner.cli` for the control-plane invariants demo.

### Drive it through the inbox (the always-on path)

```sh
python3 -m router_runner.agentd init --db /tmp/agentd.db
# write a letter (see agentd.py docstring for the schema), then:
python3 -m router_runner.agentd watch --inbox <dir> --db /tmp/agentd.db --once
python3 -m router_runner.agentd approve <work_item_id> <nonce> --db /tmp/agentd.db
python3 -m router_runner.agentd execute <work_item_id> --db /tmp/agentd.db
```

### Tests

```sh
python3 -m pytest tests/test_router_runner.py tests/test_agentd.py -q
```

## Not in v0.1 (unchanged exclusions)

LLM Eval, Threads/Subscriptions/forum, real Slurm/job submission, KGMemory,
web panel, docking stages, and the section-15 baseline defect repairs.
