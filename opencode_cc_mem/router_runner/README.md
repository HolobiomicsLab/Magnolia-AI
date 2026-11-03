# Router / Work Item prototype (vertical slice)

**Status: PROVISIONAL prototype. Every design choice here is subject to change.**
See `../docs/router-prototype-decisions.md` for today's choices and
`../docs/dsh-magnolia-v2-router-assessment.md` for the full assessment.

This package demonstrates the assessment's section-20 claim:

> One authorized event can become one traceable Work Item, one execution, one
> outcome, and one evidence-linked knowledge projection without duplication or
> loss of authority boundaries.

## What it demonstrates

- Idempotent Trigger intake (`(source_id, source_event_id)` unique)
- Deterministic admission gate (sender / project / schema / duplicates / human stop)
- Candidate Work Item + content-bound, single-use operator approval (Option A)
- Decision + Mandate created only after an admitted approval
- Legal Work Item transitions enforced in code (CAS state writes)
- Durable Effect stored **before** the external side effect (outbox pattern)
- Fake campaign adapter with pre-registered protocols only
- Execution idempotency (no duplicate campaign on retry)
- Receipted reply, evidence links (run record + gate report)
- Restart recovery from the SQLite control store
- Human stop prevents automatic resume

## Explicitly out of scope

LLM Eval, proactive automation, Threads/Subscriptions/forum, real Slurm or job
submission, KGMemory projection, web panel, and the section-15 baseline defect
repairs (those remain Phase 0 work and are NOT addressed by this package).

## Run the demo

```sh
cd opencode_cc_mem
python3 -m router_runner.cli demo --db /tmp/router-demo.db
```

The demo prints each step, including the negative cases (forged approval,
duplicate intake, illegal transition, resume after human stop).

## Run the tests

```sh
cd opencode_cc_mem
python3 -m pytest tests/test_router_runner.py -q
# or, without pytest:
python3 tests/test_router_runner.py
```

## Layout

- `models.py` — domain objects, Work Item states and legal transitions
- `store.py` — SQLite control store (single writer, transactional)
- `admission.py` — deterministic gate + approval binding
- `engine.py` — ControlEngine: intake, approval, execution, effects, evidence
- `campaign_adapter.py` — fake adapter, pre-registered protocols only
- `cli.py` — the demo driver
