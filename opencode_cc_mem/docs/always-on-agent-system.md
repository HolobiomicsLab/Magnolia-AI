# Always-on agent system (v0.1, PROVISIONAL)

Branch: `exp/always-on-agents` (renamed from `exp/nk2r-challenge` per user
decision 2026-11-03: the deliverable is the SYSTEM, not a challenge project).

All design choices are provisional and subject to change — see
`router-prototype-decisions.md` and `dsh-magnolia-v2-router-assessment.md`.

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
      nk2r-challenge-v0.1 -> nk2r_funnel (generate/filter/calibrate/rank)
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

## The NK2R challenge as the first task pack

The NK2R challenge is a *task pack*, not the system. The pack
(`nk2r-challenge-v0.1`, bound by hash to `nk2r_challenge/campaign/protocol.json`)
runs the executable funnel: 3 generators -> liability filters -> NKB
calibration -> 60 ranked candidates + run records. Docking, selectivity, and
submission remain gated exactly as documented in the pack's protocol.

### Run it end to end

```sh
cd opencode_cc_mem
rm -f /tmp/agentd-demo.db
python3 -m router_runner.agentd demo-nk2r --db /tmp/agentd-demo.db
```

Expected: letter ingested -> approval -> NK2R funnel executes ->
`outcome=completed selected=60`, all acceptance items true, candidate CSV path,
receipted reply, `DEMO-NK2R OK`.

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
python3 -m pytest tests/test_router_runner.py tests/test_nk2r_funnel.py tests/test_agentd.py -q
```

## Not in v0.1 (unchanged exclusions)

LLM Eval, Threads/Subscriptions/forum, real Slurm/job submission, KGMemory,
web panel, docking stages, and the section-15 baseline defect repairs.
