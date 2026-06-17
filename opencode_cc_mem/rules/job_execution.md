---
name: job_execution
description: Mandatory rules for running any computation — output directory convention and submit_job enforcement. Violating these forfeits lifecycle tracking and pollutes the project tree.
version: 1.0
last_verified: 2026-06-15
tags: [jobs, submit, runs, lifecycle, enforcement]
---

# Job Execution Rules

## RULE 1: All outputs go under `runs/YYYY-MM-DD_name/`

Every computation — whether a single command, a pipeline, or a Slurm job —
writes output to `projects/<name>/runs/YYYY-MM-DD_name/`. Never write results
to `raw_input/`, `/tmp/`, or the project root.

| Right | Wrong |
|-------|-------|
| `runs/2026-06-15_p2rank_4po2/` | `raw_input/receptor/p2rank_4po2/` |
| `runs/2026-06-15_kferq_docking/` | `~/kferq_output/` |

The date prefix is ISO format (`YYYY-MM-DD`). The name suffix describes what
was run.

This rule applies to: HADDOCK3, P2Rank, Gnina, xTB, GROMACS, ORCA, Gaussian,
BindCraft, BoltzGen, and any other scientific tool invoked during this project.
It also applies to read-only analysis that produces output files (plots, CSVs,
contact maps).

## RULE 2: Cluster jobs go through `submit_job` only

**Never** submit a cluster job via raw shell:

```
# FORBIDDEN
compchem-tools_run_shell("ssh azzurra 'sbatch job.sh'")
compchem-tools_run_shell("ssh azzurra sbatch --wrap='...'")
```

**Always** use the MCP tool:

```
submit_job(scheduler="ssh-slurm", tool="<tool>", ...)
```

### What raw submission forfeits

| Consequence | Impact |
|---|---|
| No local `runs/<id>.yaml` writeahead record | Run invisible to `memory_get_run_history` |
| No `.magnolia/manifest.json` on the cluster | Remote dir not self-describing |
| No auto-poll / auto-fetch / auto-assess | Manual `sacct` + `rsync` + `post_run_assess` required |
| No failure capture to staging memory | Cross-session learning loop broken |
| Tunnel not auto-started by `_ensure_tunnel()` | Must remember `hpc_tunnel.sh` manually |

### Before every `submit_job` call

Run the checks in `rules/prejob_check.md`. This is NOT optional.

## RULE 3: Run directories are self-contained

Every run directory must contain all inputs needed to reproduce the run
(copies, not symlinks). A run dir that references files outside itself
breaks reproducibility when the cluster rsyncs it to `/workspace`.

## When these rules are violated

If a run was accidentally submitted raw (as BindCraft was in June 2026):
1. Record the error via `memory_record_learning(entry_type="failure_pattern")`
2. If output exists on the cluster, fetch it with `fetch_job_results` or manual rsync
3. Create the missing run YAML manually via `memory_record_run`
4. Run `post_run_assess` on the fetched directory

## Rationale

The BindCraft runs of June 2026 were submitted via raw `ssh azzurra 'sbatch ...'`.
Twenty-three staging memory entries captured their installation struggles,
runtime behavior, and zero-design outcome — but the runs themselves left no
trace in Magnolia's run history. A future session asking "what BindCraft runs
did we do?" would find nothing through the normal tools. This rule exists so
that never happens again.
