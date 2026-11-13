# Verified one-shot results and the move of post-run assessment to compchem-tools

Date: 2026-11-13
Branch: `exp/idea-verified-run-promotion`
Origin: inbox bug report from a sister project (2026-11-13)

## Why this change exists

A bug report from a sister project showed that successful one-shot docking
results never leave staging:
`auto_promote_staging` requires `observation_count >= 2`, `confidence >= 0.85`,
and `>= 2 distinct sessions`. That is a reasonable proxy for *lessons and
patterns*, but it is the wrong evidence for *empirical results*. A finished run
is verified by its run record, not by being distilled twice.

The two examples in the ticket also showed the other half of the problem:
auto-extraction wrote no `opencode_session_id` and no run linkage, and the run
records had `status: null` because `post_run_assess` was never called.

## Why assessment now lives in compchem-tools

`post_run_assess` and its HADDOCK3 metric extraction used to live in
`compchem_memory.learning`. That put domain-specific analysis (capri files,
contact maps, residue numbering) inside the memory package, even though memory's
job is storage, retrieval, and promotion.

The split is now:

- **compchem-tools** owns analysis: technical checks, metric extraction, and
  per-tool analyzers (`compchem_tools.analysis`).
- **compchem-memory** owns the run record and the promotion gate.

`compchem-tools` already imports `ProjectManager` from `compchem-memory`, so the
dependency stays one-way: tools → memory storage. If memory's assessor had
imported the HADDOCK3 analyzer from tools, the poller would have created a
cycle: `tools.poller → memory.orchestrator → tools.haddock3`. Moving the whole
assessment layer avoids that cycle.

`compchem_memory.learning.assessor` and `learning.orchestrator` remain as thin
compatibility shims for existing imports. The `post_run_assess` MCP tool is now
exposed by `compchem-tools`.

## Analyzer registry

`compchem_tools.analysis.assessor` keeps the generic checks (run directory,
expected outputs, exit code, overall status). Tool-specific analysis is
registered in `TOOL_ANALYZERS`:

```python
TOOL_ANALYZERS = {
    "haddock3": analyze_haddock3,
}
```

`analyzers/haddock3.py` computes capri metrics **and** the standard interchain
contact / per-residue contribution analysis for every docking run. New tools
(GROMACS, ORCA, custom scripts) get generic output checks only; adding a real
analyzer means adding one module and one registry entry.

## Verified-run promotion gate

`auto_promote_staging` now has two paths:

1. **Repetition path (unchanged):** `obs >= 2`, `conf >= 0.85`,
   `distinct_sessions >= 2`.
2. **Verified one-shot path:** `obs = 1` is allowed when **all** hold:
   - `type` is `success_pattern`, `parameter_guidance`, or
     `scientific_finding` (not `note`);
   - `outcome_status` is not `intermediate`;
   - every `run_ids` record exists and is verified: `status in {"pass",
     "warning"}`, `exit_code == 0` when present, and for ssh-slurm runs
     `state == COMPLETED` and `exit_code == "0:0"`.

Confidence is intentionally **not** required for the verified path: it is a
self-declared extractor judgment, while the run record is objective technical
evidence. Verification is still not scientific correctness; it only says the
run completed and produced outputs.

## Provenance written at distillation time

- `save_candidate` and `create_entry` accept `run_ids` and store them in
  frontmatter.
- `memory_record_learning` accepts `run_ids`.
- Auto-extraction stamps `opencode_session_id = session_path.stem` and links
  `run_ids` deterministically by scanning the candidate text for run IDs that
  exist in `.magnolia/runs/`.

## Backfill

Older run records have `status: null`. Use
`compchem_tools.analysis.orchestrator.backfill_run_assessments(project_dir,
project_mgr)` to assess completed records that have a local run directory.

## Local scripts

Local analyses can use the same verified path **if** the run is recorded:
`post_run_assess(run_dir=..., tool="python", exit_code=...)` works for unknown
tools (generic output checks). Unrecorded local analyses still need repetition
or manual `memory_confirm`.

`upsert_run` now stores `exit_code`, and `assess_run` fails on non-zero exit
even when output files exist.

## Tests

- `compchem-memory/tests/test_verified_run_promotion.py`
- `compchem-tools/tests/test_haddock3_analysis.py`
- `compchem-tools/tests/test_analysis_assessor.py`
- Existing suites: compchem-memory and compchem-tools.
