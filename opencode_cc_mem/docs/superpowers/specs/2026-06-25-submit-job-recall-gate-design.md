# submit_job recall gate — read-side memory gate (step 2a)

**Date:** 2026-06-25
**Status:** Design approved, pre-implementation
**Scope:** Read-side recall gate only (2a). Auto-apply / structured-rule registry (2a-ii) and the local-job lifecycle write-side (2b) are separate follow-up specs.

## Problem

The agent re-makes mistakes the project has already paid for (wrong memory, missing chain-ID, stale dirs). The lessons exist in memory but are pull-only — nobody reads them at the moment of action. `submit_job` launches costly work with no check against what failed before. This is the read half of the "don't repeat mistakes" payoff (enforcement-gap memory `magnolia-enforcement-gap-direction`); it is the action-time retrieval that session-start retrieval cannot do (`opencode-tool-gate-contract`).

## Goal

Before `submit_job` launches, recall past pitfalls scoped to the submission's `tool`, and if any are found, **hold the submission** (return without launching), surface the lessons, and let the agent verify applicability, propose a fix, and resubmit. No compute is spent on the held attempt.

## Design decisions (settled in brainstorming)

- **Confidence-graded ladder, not binary soft/hard:** warn → propose → auto-apply, with the auto-apply threshold loosening as trust grows. `apply_tool_memory_floor` is the existing prototype of the top (auto-apply) rung. **v1 (this spec) builds the recall + surface + agent-decides loop only** — the agent performs the self-check and proposes the fix. Auto-apply (machine self-check + structured condition/fix entries) is increment **2a-ii**, deferred because auto-applying a wrong fix is the highest-risk failure mode and because today's entries are free-text (not machine-checkable).
- **Hold-once-and-confirm via an `acknowledge` arg:** stateless. A hold returns the pitfalls; the agent's resubmission with `acknowledge=True` (and any modified params) IS the decision. Chosen over stateful "surfaced-once" tracking (harder to test, fuzzy "identical resubmit") and over warn-after-launch (too late — the job already ran).
- **Recall strategy = tool-scoped warnings (Approach A):** pull `failure_pattern` / `error_resolution` entries whose `tools` include this `tool`, promoted + staging, top-N. Matches the memory design's crack-1 guidance ("surface warnings by tool/tag-in-scope, not task keywords") and asymmetric gating (surface failure-type entries immediately). Avoids the lexical-miss of keyword search and the unbuilt complexity of semantic recall.
- **All schedulers, fail-open:** the gate runs for every `submit_job` (local/slurm/pbs/ssh-slurm). Any recall error, missing `project_dir`, or `tool=None` lets the submission proceed unchanged — a memory glitch must never block a submission.

## Grounding facts (verified 2026-06-25)

- `submit_job` ([tools/jobs.py](../../mcp-servers/compchem-tools/src/compchem_tools/tools/jobs.py)) already calls `apply_tool_memory_floor(tool, ncores, memory)` at the top — the precedent for slotting a pre-launch gate in. `GATE_REGISTRY` mechanical gates exist but are not wired into submit_job.
- The cross-package import `compchem_tools → compchem_memory` is already established: `poller.py` imports `assess_and_record` from `compchem_memory.learning.orchestrator`. In-process import is the accepted pattern; no new coupling concern.
- Memory retrieval today is keyword-driven (`search_entries(keyword, tags)`, `memory_search_errors(error_message, ...)`). There is no keyword-free, tool-indexed lookup — so tool-scoped recall needs a new read-only retrieval function.
- `search_staging(project_dir, keyword, tags)` already exists and flags hits `tier="staging"`, `provisional=True` ("flag, don't hide").

## Components

### Unit 1 — `select_warnings_for_tool` (compchem_memory)

Two parts so the gate stays decoupled from `ProjectManager` internals:

(a) A `ProjectManager` method (in `tiers/project.py`, beside `search_staging` / `search_entries`):

```python
def select_warnings_for_tool(
    self, project_dir: str, tool: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Return failure_pattern / error_resolution entries whose frontmatter
    `tools` includes `tool`, from promoted entries AND staging. Staging hits are
    flagged provisional=True. Ordered promoted-before-staging, then by recency.
    Capped at `limit`. Keyword-free (tool-indexed), unlike search_entries."""
```

(b) A module-level convenience wrapper the gate imports, mirroring how
`memory_search_errors` internally resolves the store + project manager — but
returning a plain Python list (NOT a JSON string):

```python
# compchem_memory/recall.py
def warnings_for_tool(
    tool: str, project_dir: str | None = None, limit: int = 5,
) -> list[dict[str, Any]]:
    """Resolve the project store + ProjectManager, then delegate to
    ProjectManager.select_warnings_for_tool. The single entry point the
    submit_job recall gate imports."""
```

Each returned dict carries at least: `title`, `type`, `provisional` (bool), `summary` (short body excerpt), `source` (entry name/path). Empty list when no match, `tool` falsy, or store unresolvable.

### Unit 2 — `recall_gate` (compchem_tools, new `tools/recall_gate.py`)

```python
def recall_gate(
    tool: str | None, command: str, project_dir: str | None, acknowledge: bool,
) -> dict[str, Any] | None:
    """Return a hold-result dict if the submission should be held, else None to
    proceed. Never raises — any exception returns None (fail-open)."""
```

Logic, in order:
1. `acknowledge` truthy → `None` (agent already reviewed).
2. `tool` falsy or `project_dir` unresolvable → `None` (fail-open).
3. `from compchem_memory.recall import warnings_for_tool`; call `warnings_for_tool(tool, project_dir)`.
4. Hits → return the hold dict (below). No hits → `None`.
5. The whole body is wrapped in try/except → `None` on any exception (log at warning).

`command` is accepted for forward-compatibility (future keyword/secondary signal) but v1 keys only on `tool`.

### Unit 3 — wiring in `submit_job`

- Add parameter `acknowledge: bool = False` (and to the `server.py` MCP wrapper).
- Immediately after the `apply_tool_memory_floor` line, call
  `held = recall_gate(tool, command, project_dir, acknowledge)`.
- `if held is not None: return held` — before any scheduler branch, so nothing launches.

## Hold-result contract

```python
{
  "success": False,
  "held": True,
  "reason": "recall_gate",
  "tool": tool,
  "pitfalls": [ {"title": ..., "type": ..., "provisional": ...,
                 "summary": ..., "source": ...}, ... ],
  "instruction": "Relevant past pitfalls found for this <tool> submission "
    "(listed in 'pitfalls'). Verify whether each applies to THIS submission; "
    "adjust parameters if needed; then resubmit with acknowledge=True to "
    "proceed. The job was held before launch — no compute was spent.",
}
```

`success: False` + `held: True` is distinguishable from a real submission failure (which has no `held` key), so callers and telemetry can tell a hold from an error.

## Data flow (worked example)

1. `submit_job(tool="haddock3", ncores=32, memory="32GB", scheduler="ssh-slurm", ...)`.
2. `apply_tool_memory_floor` runs (existing). `recall_gate` runs: `acknowledge=False` → `select_warnings_for_tool(pd, "haddock3")` finds the OOM-on-Azzurra `failure_pattern` → returns hold dict.
3. `submit_job` returns the hold dict — no job launched.
4. Agent reads the pitfall, self-checks applicability, proposes "increase memory to 64GB" to the user.
5. Agent resubmits `submit_job(tool="haddock3", ncores=32, memory="64GB", acknowledge=True, ...)`.
6. `recall_gate` sees `acknowledge=True` → `None` → `submit_job` launches.

## Error handling

Fail-open at every step (see Unit 2). The gate must never convert a memory problem into a submission block. `recall_gate` never raises.

## Testing

- **Unit 1 (`select_warnings_for_tool`):** temp project store seeded with promoted + staging entries, some tagged for the tool, some not, mixed types. Assert: only failure_pattern/error_resolution for the tool returned; staging hits carry `provisional=True`; non-matching tools excluded; `limit` respected; promoted-before-staging ordering; empty list when none or `tool=""`.
- **Unit 2 (`recall_gate`):** `acknowledge=True` → None; no hits → None; hits → hold dict with populated `pitfalls`; `warnings_for_tool` monkeypatched to raise → None (fail-open); `tool=None` → None.
- **Unit 3 (submit_job wiring):** unacknowledged submission with a seeded pitfall returns `held=True` AND the launcher is never called (monkeypatch `_submit_local`/`_submit_slurm`/`ssh_slurm.submit` to assert not invoked); `acknowledge=True` proceeds to launch; recall error still launches (fail-open end-to-end).

## Acceptance criteria

1. An unacknowledged `submit_job` whose `tool` has a matching failure_pattern/error_resolution entry returns `{success:False, held:True, reason:"recall_gate", pitfalls:[...], ...}` and does not launch.
2. The same call with `acknowledge=True` launches normally.
3. A `submit_job` whose tool has no matching warnings launches normally (no hold).
4. Any recall failure (exception, missing project_dir, tool=None) lets the submission proceed — never blocks.
5. Staging hits are flagged provisional in `pitfalls`.
6. All tests above pass.

## Follow-ups (out of scope here)

- **2a-ii — auto-apply rung:** structured entry schema (machine-checkable condition + remediation + confidence), an in-gate deterministic self-check, and confidence-gated auto-apply (generalizing `apply_tool_memory_floor`). Plus `surfaced_in_session` suppression so a repeatedly-held tool doesn't nag within one session.
- **2b — write-side local-job lifecycle:** make `scheduler="local"` jobs create run records, get poller-scanned, and flow through `dispatch_terminal → assess_and_record` so local outcomes become learnings the gate can later recall.
- Optional secondary keyword/semantic signal layered onto tool-scoping if tool-scoping proves too coarse.
