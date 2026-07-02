# Case-based run recall — similar-system run index (step 2a-iii)

**Date:** 2026-06-26
**Status:** Design approved, pre-implementation
**Scope:** Read-side case-based recall + the minimal write-side (system tags + run-dir index) it needs. Remote-first. Builds on the shipped recall gate (2a) and the same hold/acknowledge mechanism.

## Problem

The shipped recall gate (2a) surfaces tool-scoped *warnings* before a submission. Two gaps remain:
1. **Never-seen mistakes** — a novel error matches no warning, so nothing fires. A *known-good baseline to diff against* would catch deviations no one pre-wrote a warning for.
2. **"Latest run" is the wrong baseline** — the last successful HADDOCK3 run might be a 14mer, whose sampling is wrong for the 6mer you're about to run. The useful baseline is the *most similar system*, not the most recent run. This is case-based reasoning: "find the analogous past run and point me at it."

## Goal

When submitting a job, find the most similar past run(s) of the same tool — ranked by *system similarity* — and surface a pointer to each run's directory (where the full config + inputs already live), so the agent can model its configuration on the right analog before launching.

## Key architectural decision: index, don't copy

The run **directory** already is the authoritative, complete record of a run — config file, input decks, `manifest.json`, sbatch script. Memory must hold a **pointer + a small characterization**, never a copy of the params. Copying `ncores`/`memory`/`sampling`/basis-set into the memory record would create a second source of truth that drifts (the failure mode `memory-learning-retrieval-design` warns against — "markdown tree stays source of truth"). Pointing at the dir also gets *scientific* params for free: the agent reads the real `config.cfg` / QM deck / `.mdp`, which we never parse or store.

## Design decisions (settled in brainstorming)

- **Similarity by agent-supplied system tags (v1).** The agent tags a submission with what it knows (`system_tags=["peptide","6mer","hsc70"]`). Recall ranks past runs by tag overlap. Chosen over (a) auto-extracting characteristics from inputs (per-tool parser rabbit hole) and (b) semantic embeddings (the deferred hybrid-retrieval; overkill at this corpus size). Both are later upgrades behind the same `similar_runs` interface.
- **Surface as a one-shot HOLD, not inform-only.** The plugin API offers only hold-or-proceed before launch (no "inform without stopping"); inform-only would arrive after launch — too late to fix *this* run, defeating the catch-novel-mistakes purpose. So "a similar prior run exists" is a hold trigger, unified with the warning gate, cleared by the existing `acknowledge=True`. (This intentionally overrides the asymmetric-gating "success→inform" guidance, which was about not *blocking on an unproven success*; here a past run is a config reference and the one-shot hold is what makes it land pre-launch.)
- **Index, not copy** (see above): record `{tool, status, run_dir, system_tags}`.
- **Remote-first.** Remote runs already carry `run_dir` (`remote.local_run_dir`) and a success status. Local runs get no record/status yet (the 2b gap); local participation rides 2b. v1 works for remote; local degrades gracefully (simply has no candidates until 2b).
- **Fail-open**, like the existing gate: any recall error → no hold, submission proceeds.

## Grounding facts (verified 2026-06-26)

- `ProjectManager.record_run(project_dir, run_id, tool, status, metrics, quality_flags, errors_solved, *, lifecycle, remote)` writes the run-record YAML. It stores outcome + `remote` but **no submission params and no system tags** — `system_tags` is a new field to add.
- ssh-slurm submit (`ssh_slurm.submit`) calls `record_run(...)` and already stores `remote.local_run_dir`, `remote.partition/account/qos/cluster`, and writes a `manifest.json` (with `command`, scheduler params) into the run dir. It does **not** store `ncores`/`memory` in the record (only in the sbatch script in the dir) — fine, the index model doesn't need them.
- `memory_get_run_history(project_dir)` → `ProjectManager.get_run_history(pd)` returns the list of run records. This is the read source for ranking.
- Run status: `assess_run` sets `overall ∈ {pass, warning, fail}`; in-flight records have `status=None`. "Successful" = `status ∈ {pass, success, warning}` (completed without failure); `None`/`fail`/`failed` excluded.
- Shipped recall gate: `recall_gate(tool, command, project_dir, acknowledge)` in `compchem_tools/tools/recall_gate.py`, called in `submit_job` after `apply_tool_memory_floor`, returns a hold-dict or None; `warnings_for_tool` in `compchem_memory/recall.py`.

## Components

### Unit 1 — capture `system_tags` (write side)

- Add `system_tags: list[str] | None = None` to `submit_job` (jobs.py) and the `server.py` MCP wrapper (forwarded).
- Add `system_tags: list[str] | None = None` to `ProjectManager.record_run`; persist it as a top-level `system_tags` field in the YAML (default `[]`).
- Thread `system_tags` from `submit_job` → `ssh_slurm.submit` → `record_run(system_tags=...)` for the remote path. (Local path creates no record in v1; deferred to 2b.)

### Unit 2 — `similar_runs` (read side)

In `compchem_memory` (recall.py module wrapper + a `ProjectManager` method, mirroring the `warnings_for_tool` / `select_warnings_for_tool` split):

```python
# ProjectManager
def similar_runs(self, project_dir, tool, system_tags, limit=3) -> list[dict]: ...
# recall.py wrapper the gate imports
def similar_runs(tool, system_tags, project_dir=None, limit=3) -> list[dict]: ...
```

Behavior: from `get_run_history(pd)`, keep records where `record["tool"] == tool` and status is successful (`∈ {pass, success, warning}`); score each by **tag overlap** with `system_tags` (count of shared tags, case-insensitive; tie-break by recency); drop zero-overlap records; return top-`limit` as dicts `{run_id, run_dir, system_tags, date, status, score, overlap}` where `run_dir` is `remote.local_run_dir` (or the record's dir field). Empty list when `tool`/`system_tags` falsy, no matches, or store unresolvable.

### Unit 3 — extend `recall_gate` to add the similar-run hold

- New signature: `recall_gate(tool, command, project_dir, acknowledge, system_tags=None)`.
- `acknowledge` truthy → None (unchanged). Else gather BOTH: `warnings_for_tool(...)` (existing) and `similar_runs(tool, system_tags, project_dir)` (new).
- If warnings OR similar-runs exist → return a hold-dict carrying both: existing `pitfalls` plus a new `similar_runs: [{run_dir, system_tags, date, score}]` and an `instruction` that tells the agent to (a) heed the pitfalls and (b) inspect the most similar run dir(s) to model config, then resubmit with `acknowledge=True`.
- If neither → None (proceed).
- Whole body fail-open (any exception → None), unchanged.

### Unit 4 — wiring

`submit_job` passes `system_tags` into `recall_gate(tool, command, project_dir, acknowledge, system_tags)`. (The call site already exists from 2a; add the arg.) `server.py` wrapper forwards `system_tags`.

## Hold-result contract (extended)

```python
{"success": False, "held": True, "reason": "recall_gate", "tool": tool,
 "pitfalls": [ ... ],                                  # may be empty
 "similar_runs": [ {"run_dir", "system_tags", "date", "score"} ],  # may be empty
 "instruction": "Found relevant context for this <tool> submission. If "
   "'pitfalls' is non-empty, verify each applies. If 'similar_runs' is "
   "non-empty, inspect the most similar run directory and model your "
   "configuration on it (its config/inputs are saved there). Then resubmit "
   "with acknowledge=True. Held before launch — no compute spent."}
```
At least one of `pitfalls` / `similar_runs` is non-empty whenever a hold is returned.

## Data flow (worked example)

1. `submit_job(tool="haddock3", command=..., system_tags=["peptide","6mer","hsc70"], scheduler="ssh-slurm")`.
2. `recall_gate`: `acknowledge=False`. No warnings, but `similar_runs` finds a past 7mer-peptide-hsc70 run (overlap 2) at `/…/runs/2026-06-10_hsc70_7mer/` → hold.
3. `submit_job` returns the hold (no launch). Agent opens the 7mer run dir, sees its `sampling`, adjusts for a 6mer, resubmits with `acknowledge=True`.
4. `recall_gate` sees `acknowledge=True` → None → launches; `record_run` stores `system_tags=["peptide","6mer","hsc70"]` + `run_dir`, indexing this run for the future.

## Error handling

Fail-open everywhere (Unit 3 wraps its body). `record_run` with `system_tags` defaults to `[]` and never changes existing-record behavior when the arg is absent.

## Testing

- **Unit 1:** `record_run(..., system_tags=[...])` persists the field; absent arg → `[]`; existing callers unaffected.
- **Unit 2 (`similar_runs`):** seed run history with same-tool runs of differing tags + statuses; assert ranking by tag overlap, zero-overlap excluded, failed/in-flight excluded, `limit` respected, `run_dir` returned, empty on falsy/no-match.
- **Unit 3 (`recall_gate`):** similar-run present + no warnings → hold with populated `similar_runs`; warnings + similar both → both in hold; neither → None; `acknowledge=True` → None; `similar_runs` raises → still returns (fail-open) using warnings only.
- **Unit 4 (wiring):** `submit_job` with a seeded similar run is held and does not launch (launcher monkeypatched, asserted not called); `acknowledge=True` launches and records `system_tags`.

## Acceptance criteria

1. A submission whose `tool`+`system_tags` matches a prior successful run is held with a `similar_runs` pointer and does not launch.
2. `acknowledge=True` launches and the run is recorded with its `system_tags` + run_dir (remote).
3. A submission with no warnings and no similar runs launches normally.
4. Ranking favors higher tag overlap; failed/in-flight/zero-overlap runs are never offered.
5. Any recall failure lets the submission proceed.
6. All tests pass.

## Follow-ups (out of scope)

- **Session suppression** of repeat similar-run holds (the `surfaced_in_session` mechanism) — deferred to keep parity with the shipped stateless gate; v1 holds once per submit, cleared by `acknowledge`.
- **2b** — local-job lifecycle: gives local runs records + success status so they enter the same index.
- **Auto-extracted system tags** (from inputs) and **semantic similarity** — upgrades behind the `similar_runs` interface.
- **Auto-apply (M)** and **param-shape guards (P)** — the other 2a-ii branches, still designed-not-built.
