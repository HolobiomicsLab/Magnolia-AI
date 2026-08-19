---
name: magnolia
version: 2.1
description: General agent behavior rules for Project Magnolia
---

# Magnolia Agent Behavior Rules

## Discussion vs. execution

Not every message is a command to act. The user is often thinking out loud,
weighing options, or asking what is possible — **before** deciding what to do.
Acting in the middle of that wastes runs, pollutes the session log, and pre-empts
a decision the user has not made yet.

**Default to proposing, not doing.** When a turn is exploratory, respond with
analysis and a concrete proposed plan, then stop and let the user confirm.

**Signals the user is still discussing — do NOT start executing:**

- Questions: "do we need to…", "should we…", "what about…", "is there a way…",
  "why does…", "can you explain…".
- Comparisons or open options on the table with no choice made.
- "I'm thinking…", "I wonder…", or any message with no imperative verb directed
  at you.

**What is always allowed during discussion:** read-only investigation that
informs the conversation — reading files, `ls`, inspecting logs, and
`memory_get_context`. These do not mutate state and need no go-ahead.

**What requires an explicit go-ahead:** anything that changes state or launches
work — editing or writing files, `compchem-tools_run_shell` commands that mutate (installs,
moves, deletes, runs), `submit_job`, and `memory_record_learning` /
`memory_confirm`. Do these only when the user gives a clear instruction to act
("do it", "run it", "go ahead", "fix it") or the original request was itself an
unambiguous imperative.

**When unsure, ask.** A one-line "Want me to go ahead with X?" is cheaper than
an unwanted action. During collaborative work, take one step, report, and pause
— do not chain several mutating actions before the user has seen the first.

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

**Verify calculations before presenting.** Computed values (AIR-free scores,
energy differences, RMSD deltas) must be verified by a script or explicit
step-by-step arithmetic before appearing in a table. Never present a value
computed mentally — write the calculation out or run a one-liner.

**Parameter recommendations must be grounded in prior runs for the same tool
and project.** Before recommending a numeric parameter (sampling, ncores,
timelimit, etc.), check at least one prior run result to support the value.
For example, before proposing a HADDOCK3 sampling level, check the cluster
sizes from past runs — if 10000 models gave n=4 clusters, recommending 2000
without citing that fact is speculation, not a recommendation. State the
prior result explicitly: "At s10000 we got n=4, so s5000 is a reasonable
starting point."

When in doubt, under-claim and name the missing analysis.

## Shell commands

Opencode's bash tool is disabled. **Use `compchem-tools_run_shell` for every shell command**;
it invokes `magnolia-run`, which logs the command, exit code, and working
directory to `.magnolia/sessions/` and fires auto-assessment for recognized
scientific tools.

**Foreground calls are capped at 110 s (default 90 s)** — the server always
answers before the MCP client aborts the call. Never run anything that may
block longer in the foreground; use one of:

- `background=True` — detaches the command and returns `pid` + `log_file`
  immediately (log under `/tmp/magnolia-shell-bg/`). Poll by reading the log.
- `submit_job(scheduler="local")` — tracked lifecycle, for real workloads.
- Long HPC work → `submit_job(scheduler="ssh-slurm")` per `rules/job_execution.md`.

**Examples:**

```python
compchem-tools_run_shell(cmd="haddock3 config.cfg")
compchem-tools_run_shell(cmd="gmx mdrun -deffnm md")
compchem-tools_run_shell(cmd="ls runs/")
compchem-tools_run_shell(cmd="sleep 600 && echo done", background=True)
```

## Memory

Refer to `AGENTS.md` for when to call each memory tool. The key points:

- Start every task with `memory_get_context`.
- After a fix, call `memory_record_learning(entry_type="error_resolution", ...)`.
- After a noteworthy result, call `memory_record_learning(entry_type="success_pattern", ...)` with a mandatory CAVEAT.
- After a confirmed failure mode, call `memory_record_learning(entry_type="failure_pattern", ...)`.

## Long-running jobs

Long-running jobs (>30 min) MUST be submitted via `submit_job` (local or
Slurm) rather than run in the foreground. See `rules/job_execution.md` for
mandatory output directory conventions and the prohibition on raw `ssh sbatch`.

**Before every `submit_job` call, run the checks in `rules/prejob_check.md`.**
This is mandatory — never skip input verification before submitting.

For HPC submission to the Azzurra cluster — VPN setup, SSH conventions,
partition choice, Slurm patterns — see `rules/hpc_azzurra.md`.

## Project structure

- All run output goes to `runs/YYYY-MM-DD_name/`. See `rules/job_execution.md`.
- Keep all inputs, runs, and memory inside `projects/<name>/`.
- Use `softwares/bin/` wrappers for tool invocation.
- Respect the `.gitignore` boundaries: do not track heavy environments or caches.
