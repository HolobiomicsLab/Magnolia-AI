---
name: magnolia
version: 2.0
description: General agent behavior rules for Project Magnolia
---

# Magnolia Agent Behavior Rules

## Grounding claims in data

This is a research project. Every analytical claim you make to the user must be
traceable to a specific artifact in the run directory or session log. Speculation
dressed as a finding wastes the user's time and corrupts downstream decisions.

**Required for every claim:**

- Cite the source. A score: the file + run name (e.g.
  `runs/2026-05-15_E8F/output/.../caprieval`). A contact / mode statement: the
  contact-map or energy-decomposition analysis that produced it. A parameter
  effect: the two runs being compared.

- If the data needed to support a claim does not exist yet, say so explicitly.
  Do NOT fill the gap with mechanistic reasoning. Use language like: "We have
  AIR-free scores for X but no contact-map analysis — would need to run
  contactmap on run Y to distinguish (a) from (b)."

- Separate **observation** from **hypothesis**. Observations cite data;
  hypotheses are flagged ("Hypothesis:", "One possible mechanism:") and must
  list what evidence would confirm or refute them.

- Quantitative claims need numbers, not adjectives. "Score worsened by +14.2"
  not "score worsened significantly." Residue / position claims need the
  residue number and the chain.

**Red flags — stop and check before stating:**

- Structural / mechanistic explanations for results where only scalar scores
  were measured.
- Comparisons across mutants when only some have full analyses (contact map,
  energy decomp, RMSD).
- Generalizations from one pocket / one protocol / one binding mode without a
  CAVEAT.
- The word "likely" or "presumably" without a follow-up sentence saying what
  would settle it.

When in doubt, under-claim and name the missing analysis.

## Shell commands

Opencode's bash tool is disabled. **Use `run_shell` for every shell command**;
it invokes `magnolia-run`, which logs the command, exit code, and working
directory to `.magnolia/sessions/` and fires auto-assessment for recognized
scientific tools.

**Examples:**

```python
run_shell(cmd="haddock3 config.cfg")
run_shell(cmd="gmx mdrun -deffnm md")
run_shell(cmd="ls runs/")
```

## Memory

Refer to `AGENTS.md` for when to call each memory tool. The key points:

- Start every task with `memory_get_context`.
- After a fix, call `memory_record_learning(entry_type="error_resolution", ...)`.
- After a noteworthy result, call `memory_record_learning(entry_type="success_pattern", ...)` with a mandatory CAVEAT.
- After a confirmed failure mode, call `memory_record_learning(entry_type="failure_pattern", ...)`.

## Long-running jobs

Long-running jobs (>30 min) should be submitted via `submit_job` (local or
Slurm) rather than run in the foreground.

For HPC submission to the Azzurra cluster — VPN setup, SSH conventions,
partition choice, Slurm patterns — see `rules/hpc_azzurra.md`.

## Project structure

- Keep all inputs, runs, and memory inside `projects/<name>/`.
- Use `softwares/bin/` wrappers for tool invocation.
- Respect the `.gitignore` boundaries: do not track heavy environments or caches.
