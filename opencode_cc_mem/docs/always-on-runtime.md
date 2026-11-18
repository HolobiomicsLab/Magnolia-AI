# Always-on runtime v0.1 — opt-in legacy mailbox bridge

Status: implemented prototype on `exp/always-on-runtime`.

This is the integration layer described in `always-on-agent-system.md`, not a
replacement for that document's availability-first `agentd` path. It joins
conversation events, Work Items, legacy mailbox execution, bounded memory,
timers, and replies while preserving the existing daemon as the only OpenCode
executor.

## What is new

```text
inbox/always-on/*.json       followup/schedule timers
            |                       |
            +------- durable events +
                        |
                 Work Item + approval
                        |
            one batched run per lane
                        |
        inbox/from-<lane>/<run>.task.md
                        |
          EXISTING magnolia-agents-daemon
                        |
              sibling *_reply.md file
                        |
        Work Item outcome + progress + durable outbox
```

The runtime supplies:

- durable, idempotent event intake;
- one active run per lane, enforced by a SQLite partial unique index;
- batching of pending events for one Work Item;
- follow-up and recurring timers stored separately from ready events;
- linked Work Items when a terminal conversation receives new work;
- optional per-thread OpenCode sessions instead of one session per lane;
- bounded boot-context/progress handoff without promoting anything to memory;
- response-level `waiting_human` and no-send outcomes;
- durable, idempotent outbox delivery.

## Backward compatibility

The default path is unchanged:

- no existing `agent.json` is modified;
- no existing inbox is watched by the new runtime unless explicitly started;
- `magnolia-agents-daemon` still discovers and executes the same task letters;
- letters without `session-key` use the original `lanes.<agent>.session_id`;
- existing five-argument test/custom runners receive no new keyword arguments;
- `agentd run`, `agentd watch`, campaign execution, and the old scheduled sweep
  and `wait_for` mechanisms retain their previous behavior;
- the new tables are additive (`ao_*`) and use `CREATE TABLE IF NOT EXISTS`.

The only legacy **mailbox** behavior change is optional `session-key` handling.
New runtime letters carry it; old letters do not. Thread sessions live in a
separate `lanes.<agent>.sessions` mapping and never overwrite the legacy lane
session.

Shared control-plane hardening is also additive at the API level:

- `ControlStore.tx()` now composes nested operations with an outer
  `BEGIN IMMEDIATE` and inner savepoints instead of allowing an inner operation
  to commit independently. Existing single-operation callers keep their public
  interface, including legacy direct `insert`/`update` use.
- `ControlEngine.execute()` gives an adapter an optional preflight `validate()`
  hook before creating an assignment or Effect. Adapters without the hook are
  unaffected.
- `legacy-mailbox-v0.1` is registered solely as an approval identity. The
  campaign adapter rejects it before mutation; mailbox events likewise reject
  campaign/task-pack protocols so neither path can masquerade as the other.

## Enable a lane

The lane must already have `agent.json`. Add the nested object yourself:

```json
{
  "enabled": true,
  "model": null,
  "always_on": {
    "enabled": true,
    "auto_execute": false,
    "delivery_dir": "inbox/always-on-outbox",
    "memory_limit": 12000
  }
}
```

`always_on.enabled` defaults to false. `auto_execute` also defaults to false:

- **false:** the first Work Item in a thread waits for content-bound operator
  approval. Related active-thread events cannot change protocol, scope, or
  budget.
- **true:** the operator-authored lane policy approves the exact WorkSpec and
  records the issuer as `policy:always-on:<lane>`. This is the only automatic
  approval path; event JSON cannot enable it.

A human `STOP` always wins over `auto_execute`: a later request opens a fresh
candidate and waits for manual approval rather than automatically resuming the
stopped thread.

Use a separate database per lane/tree. The runtime intentionally does not call
`ControlEngine.recover()`: its active attempts belong to the mailbox daemon and
are tracked in `ao_runs`, not campaign `assignments`.

## Events

Write JSON files into `<project>/inbox/always-on/`:

```json
{
  "source_id": "inbox:operator",
  "source_event_id": "message-0001",
  "thread_key": "memory-improvement",
  "text": "Investigate the retrieval regression.",
  "work_spec": {
    "project": "demo",
    "protocol": "legacy-mailbox-v0.1",
    "scope": "one investigation thread; existing mailbox permissions",
    "budget": "one headless OpenCode run per batch"
  }
}
```

`work_spec` may be omitted; the values above are the defaults apart from the
deployment-specific project name. `actor` defaults to the configured operator.
Other actors and cross-project execution are rejected. Repeated source identity
returns the original event without creating another Work Item.

Events arriving together for one active Work Item are batched into one letter.
Events for another thread wait while that lane has an active run. If an event
arrives during a run, it joins the current Work Item as context-only input; if
that run finishes first, the pending event receives a linked Work Item rather
than resurrecting the terminal one.

Changed protocol, scope, or budget is rejected while a Work Item is active.
After it becomes terminal, such a change is allowed in the same conversation
thread but creates a fresh Work Item and approval.

## Run it

From `opencode_cc_mem/`:

```bash
# One deterministic sweep: ingest, fire timers, reconcile, bridge, dispatch.
python3 -m router_runner.always_on \
  --db /tmp/demo-always-on.db --project-dir projects/demo run --once

# Continuous availability loop. This process does not call an LLM.
python3 -m router_runner.always_on \
  --db /tmp/demo-always-on.db --project-dir projects/demo run --interval 10

# Inspect pending work and approval nonces.
python3 -m router_runner.always_on \
  --db /tmp/demo-always-on.db --project-dir projects/demo status

# Manual approval, then bridge on the next sweep.
python3 -m router_runner.always_on \
  --db /tmp/demo-always-on.db --project-dir projects/demo \
  approve <work-item-id> <nonce>

# Single event/timer ingestion is also available for adapters and cron.
python3 -m router_runner.always_on \
  --db /tmp/demo-always-on.db --project-dir projects/demo ingest event.json
python3 -m router_runner.always_on \
  --db /tmp/demo-always-on.db --project-dir projects/demo timer timer.json
```

Keep the normal `magnolia-agents-daemon` running. The runtime only writes the
ordinary `*.task.md` letter; the legacy daemon performs the OpenCode run,
applies the `FINAL ANSWER` gate, writes the reply, and changes letter status.

## Timers

A one-off follow-up:

```json
{
  "kind": "followup",
  "due_at": "2026-11-16T09:00:00+00:00",
  "thread_key": "memory-improvement",
  "text": "Check whether the evaluation has replied."
}
```

A recurring schedule adds `recurrence_seconds`:

```json
{
  "kind": "schedule",
  "due_at": "2026-11-16T09:00:00+00:00",
  "recurrence_seconds": 86400,
  "thread_key": "literature-maintenance",
  "text": "Run the scheduled literature sweep."
}
```

Timers are not ready events. A sweep fires due timers into the event table;
follow-ups become done and schedules advance beyond the current time. Missed
recurrences coalesce into one event. Timer event identity includes the timer id
and scheduled occurrence, so retries do not duplicate work.

Admission failure is isolated per timer: `last_error` and `error_count` are
updated, the occurrence stays due for an explicit fix/retry, and other timers
and events continue. A schedule is never advanced past an event that failed
admission. The continuous loop also logs unexpected sweep errors instead of
silently exiting.

## Memory boundary

Each generated letter contains at most `memory_limit` characters from:

1. `.magnolia/boot-context.md`;
2. `.magnolia/session-notes/always-on/<thread>.md` for previous progress.

After a reply, the runtime writes the latter as **guidance-only working state**
with a pointer to the full reply. It never writes `staging/`, `entries/`, or
`rules/`; auto-promotion is unchanged. The prompt explicitly says memory cannot
expand approved scope. Headless helpers still follow the legacy restriction on
memory tools and file writes.

## Reply and attention contract

The existing `FINAL ANSWER:` contract remains mandatory. Two optional final
lines add runtime semantics:

```text
always-on-status: waiting-human
```

The answer should contain one concrete question. The Work Item enters
`waiting_human`, the outbox kind becomes `question`, and the user's next event
resumes the same Work Item and thread session.

```text
always-on-no-send: yes
```

The reply and progress are retained, but the outbox row is `suppressed` and is
never dispatched.

Ordinary replies become pending outbox rows. If `delivery_dir` is configured,
a sweep writes one Markdown file per message and then marks it sent. Existing
files and repeated dispatch calls do not duplicate delivery. Without a delivery
directory, replies remain pending and the original legacy reply file is still
available.

## Restart behavior

- Trigger creation, Work Item creation, approval, and event linkage compose in
  one database transaction. Recovery also recognizes older partial states and
  releases `waiting_approval` events whose Work Item is already queued.
- Pending input automatically resumes a `waiting_human` Work Item, including
  input that arrived before the previous run asked its question.
- A durable `bridging` row precedes every mailbox-letter side effect.
- A crash before letter creation is safe: reconciliation emits the same run
  letter once. Publication uses atomic no-replace creation, so a late recovery
  publisher cannot overwrite a letter the daemon has already completed.
- A crash after letter creation reuses that letter, including a reply that may
  already have arrived. The `bridging → running` update is a conditional state
  transition and cannot resurrect a finalized run.
- If a **running** letter disappears, the run fails closed. It is not blindly
  re-emitted because the agent may already have acted.
- Reply reconciliation, progress writes, and outbox insertion are idempotent
  per run. Finalization re-reads the active run under a write transaction, and
  dispatch serializes pending-message claims across processes.
- The SQLite unique active-run index prevents two runtime processes from
  bridging simultaneous runs for one lane.
- Successful, failed, and waiting Work Items receive a matching `outcome` in
  the same transaction as their state transition.

## Deliberate limits of v0.1

- No direct MCP memory reads/writes in headless workers.
- No auto-mode safety classifiers or general credential proxy.
- No arbitrary mid-tool-call deferral; deferral is response-level only.
- No HTTP webhook handler or multi-user routing; deployment adapters write
  durable JSON events.
- No automatic timer creation from model output.
- No attention-quality eval yet; no-send and delivery state are mechanisms, not
  evidence that message volume is well tuned.
- No change to campaign execution authority or autonomous scientific claims.

## Tests

```bash
python3 -m pytest \
  tests/test_always_on_runtime.py tests/test_agents_daemon.py \
  tests/test_agentd.py tests/test_agentd_loop.py tests/test_recovery.py \
  tests/test_router_runner.py tests/test_dreaming_taskpack.py -q
```

The new tests use temporary SQLite stores and fake runner responses. One test
crosses the real legacy `handle_task` filesystem boundary; none calls an LLM,
OpenCode, a cluster, or the network.
