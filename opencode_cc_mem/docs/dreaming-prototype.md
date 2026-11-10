# Dreaming pass — prototype (exp/dreaming-pass)

Offline re-check of memory entries against their source session transcripts,
executed as a bounded Work Item by the always-on agent.

Origin: the 2026-11-09 discussion of Anthropic's *The components of agent
memory*. Distillation writes memory forward, in-session; dreaming reads it
backward and checks it against the transcript that produced it. A note that was
wrong from the start, or that drifted, can then be corrected before a later
session trusts it.

## What it does

```
.dirty-set selection (anchored entries only, capped)
        ↓
transcript resolution (opencode export → SessionManager JSONL)
        ↓
LLM judge: confirmed | stale | wrong | unverifiable
        ↓
deterministic apply (hash-guarded, append-only, capped)
        ↓
receipts + versioning commit
```

- **Anchors only.** Entries need `opencode_session_id` and/or
  `observed_in_sessions`. An entry without a transcript is skipped, never
  guessed at.
- **Model proposes, code applies.** The judge returns a verdict; only
  deterministic code mutates the store.
- **Hash guard.** Every mutation re-reads the entry and requires the
  proposal-time SHA-256 to match. A file a human edited in the meantime is
  dismissed, not overwritten.
- **Append-only corrections.** `wrong` adds a `## Dreaming correction` section
  with corrected text, evidence quotes, source sessions, and rationale, plus a
  `dreaming_status: corrected` stamp. `stale` only stamps
  `dreaming_status: stale`. Nothing is deleted or silently rewritten.
- **Evidence required.** A `wrong` verdict without `corrected_text` or evidence
  is never applied.
- **Receipts.** One JSONL row per processed proposal in
  `.magnolia/reflex/dreaming-log.jsonl`; entry changes are committed to the
  store's versioning repo.

## Bounds (hashed into the protocol, covered by approval)

`router_runner/protocols/memory-dreaming-v0.1.json`:

| Bound | Default |
|---|---|
| `max_entries` per pass | 20 |
| `max_sessions_per_entry` | 3 |
| `max_transcript_chars` | 60,000 |
| `max_entry_chars` | 12,000 |
| `max_apply` (mutations per pass) | 10 |
| `recheck_days` (skip recently checked entries) | 7 |

## Run it

Offline demo (throwaway store, judge forced off):

```bash
cd opencode_cc_mem
python3 -m router_runner.agentd demo-dreaming --db /tmp/dreaming-demo.db
```

Against a real project, through the normal Work Item flow:

```bash
# out_root must be <project>/runs; the pass fails closed if <project>/.magnolia
# is absent — it never creates or guesses a memory store.
python3 -m router_runner.agentd ingest letter.json --db /tmp/agentd.db
python3 -m router_runner.agentd approve <work_item_id> <nonce> --db /tmp/agentd.db
python3 -m router_runner.agentd execute <work_item_id> --db /tmp/agentd.db \
    --out-root <project>/runs
```

`agentd.DREAMING_LETTER` is the demo letter for protocol `memory-dreaming-v0.1`.
Approval binds the exact protocol hash, so the bounds above are part of what is
approved.

Directly from the memory package:

```python
from compchem_memory.dreaming import DreamingConfig, run_dreaming
summary = run_dreaming("/path/to/project", config=DreamingConfig(max_entries=20))
```

Dry run / offline (no LLM, everything comes back unverifiable, no mutations):

```bash
MAGNOLIA_DREAMING_JUDGE=off python3 -m router_runner.agentd demo-dreaming
```

## Artifacts

| File | Content |
|---|---|
| `.magnolia/reflex/dreaming-proposal.json` | per-entry verdicts + proposal-time hashes; the review and crash-recovery unit |
| `.magnolia/reflex/dreaming-log.jsonl` | one receipt per processed proposal |
| `<out_root>/<work_item_id>/attempt-N/run-record.json` | the pass summary, used for reconcile |
| `<out_root>/<work_item_id>/attempt-N/dreaming-report.md` | human-readable per-entry report + acceptance flags |

Re-running a pass after a crash is safe: apply is hash-guarded and corrections
are append-only, so a finished correction cannot be applied twice.

## What this prototype is NOT

- **Not an integrity check.** If the agent can edit its own transcripts, the
  "ground truth" dreaming validates against is forgeable (see the
  trace-tampering digest). Treat verdicts as consistency findings until
  transcripts are append-only or hash-chained.
- **Not auto-scheduled.** `agentd run_loop()` still never executes work itself;
  a dreaming Work Item goes through the same explicit approve/execute path as
  every other pack. A dirty-set trigger and a nightly schedule are the intended
  next step, not part of this prototype.
- **Not a wiki layer.** The representation layer over digests/notes is a
  separate, slower Work Item, with its own bloat guard.
- **Not a replacement for distillation.** The ~20-minute distillation timer is
  untouched; dreaming is a sibling reflex pass, not a change to capture.

## Files

- `mcp-servers/compchem-memory/src/compchem_memory/dreaming.py` — selection,
  transcript resolution, judge, deterministic apply, receipts, report.
- `mcp-servers/compchem-memory/tests/test_dreaming.py` — 12 tests.
- `router_runner/taskpacks.py` — `memory-dreaming-v0.1` registration, runner,
  reconcile.
- `router_runner/protocols/memory-dreaming-v0.1.json` — hashed bounds.
- `router_runner/agentd.py` — `DREAMING_LETTER`, `demo-dreaming` subcommand.
- `tests/test_dreaming_taskpack.py` — 4 tests (registration, fail-closed,
  end-to-end Work Item execution, reconcile).

## Test status (2026-11-10)

- `uv run pytest tests/test_dreaming.py -q` → 12 passed
- `python3 -m pytest tests/test_dreaming_taskpack.py -q` → 4 passed
- `python3 -m pytest tests/test_agentd.py tests/test_agentd_loop.py tests/test_recovery.py -q` → 12 passed
- `cd mcp-servers/compchem-memory && uv run pytest -q` → 808 passed
