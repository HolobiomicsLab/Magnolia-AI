---
name: slurm
description: Cluster-agnostic Slurm primer — sbatch directives, state machine, sacct semantics, common gotchas. Cluster-specific facts (partition names, account names, modulefiles) live in rules/hpc_<cluster>.md.
version: 1.0
last_verified: 2026-05-28
tags: [slurm, hpc, scheduler]
---

# Slurm Operating Rules

This rule covers **Slurm semantics that are the same on every cluster Magnolia
operates on**. Per-cluster details — VPN/SSH access, exact partition names,
account names, module conventions — live in `rules/hpc_<cluster>.md` (e.g.
`rules/hpc_azzurra.md`).

Slurm is the workload manager. You submit a *job script* via `sbatch`; Slurm
schedules it onto compute nodes based on resource requests; you observe state
via `squeue` (live) and `sacct` (historical).

## When to use Slurm

Match `magnolia.md`'s long-running-jobs rule: route any workload whose expected
wall time exceeds 30 min through Slurm rather than running it in the foreground.
Choose partition by **expected wall time first**; memory and CPU shape are
secondary because most clusters have multiple comparable-CPU partitions
distinguished mainly by walltime caps.

## The five things every Slurm job needs

A minimal `sbatch` script must specify:

```bash
#!/bin/bash
#SBATCH --job-name=<descriptive>       # appears in squeue / sacct / output filenames
#SBATCH --account=<your-account>       # who pays — required on most clusters
#SBATCH --qos=<your-qos>               # quality-of-service / priority class
#SBATCH --partition=<partition>        # which physical resource
#SBATCH --time=HH:MM:SS                # walltime cap (must fit the partition's max)
#SBATCH --nodes=1                      # how many nodes
#SBATCH --ntasks=1                     # how many MPI ranks (or processes)
#SBATCH --cpus-per-task=N              # threads per rank (OpenMP / fork-parallel)
#SBATCH --mem=<size>                   # OPTIONAL — many partitions allocate per-CPU
#SBATCH --output=%x_%j.out             # stdout: %x=job-name, %j=job-id
#SBATCH --error=%x_%j.err              # stderr

module purge
module load <your-tool-modules>
<your-actual-workload>
```

The cluster-specific `rules/hpc_<cluster>.md` lists the valid `<your-account>`,
`<your-qos>`, and `<partition>` values for that cluster.

## Account vs Partition vs QOS — three independent axes

These three concepts are often conflated by first-time users. They're independent:

| Axis | What it means | How to discover yours |
|---|---|---|
| **Account** | Which research project / billing line the time draws from | `sacctmgr show association where user=$USER format=Account,QOS` |
| **Partition** | Which physical hardware (node group) the job lands on | `sinfo -s` |
| **QOS** | Priority class + limits (max jobs, max walltime, max CPUs) — typically tied to your account | `sacctmgr show qos format=Name,Priority,GrpTRES,MaxTRESPerUser` |

A job needs **all three to be valid AND mutually compatible** for it to run:
- The account must be allowed on the chosen partition (`AllowAccounts` in `scontrol show partition`)
- The QOS must permit the requested resources
- A common silent failure: your account defaults to a fallback like `users` with QOS `suspended`, which rejects every submission with `QOSGrpCpuLimit` or similar. Pass `--account=…` and `--qos=…` explicitly to override.

## --cpus-per-task vs --ntasks vs --ntasks-per-node

The three knobs that decide *how* your job consumes CPUs:

| Directive | Meaning | When to use |
|---|---|---|
| `--ntasks` | Number of MPI ranks (or "tasks", or independent processes) | MPI applications, embarrassingly-parallel CPython multiprocessing |
| `--cpus-per-task` | Threads per task (OpenMP, pthreads, Python `concurrent.futures`) | Single-process programs that internally parallelize via threads |
| `--ntasks-per-node` | Distribute N MPI ranks per node | MPI jobs across multiple nodes |

Typical patterns:
- Single-threaded script: `--ntasks=1 --cpus-per-task=1`
- Single-process multi-threaded (e.g. xtb with `OMP_NUM_THREADS=4`): `--ntasks=1 --cpus-per-task=4`
- MPI: `--ntasks=N --cpus-per-task=1` (or `--nodes=K --ntasks-per-node=N/K`)
- Hybrid MPI+OpenMP: `--ntasks=M --cpus-per-task=N` (M ranks, each running N threads)

Slurm exports `$SLURM_CPUS_PER_TASK` inside the job; chain it through to your tool's threading knob (e.g. `export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK`).

## --gres for GPUs

GPUs are a **generic resource** (`gres`), separate from CPUs:

```bash
#SBATCH --gres=gpu:1                # 1 GPU of whichever type the partition offers
#SBATCH --gres=gpu:a100:1           # specifically 1 A100 (cluster must advertise this type)
```

Useful CPU-only-on-GPU-partition trick: if a GPU partition has mostly-allocated
GPUs but lots of idle CPUs (the common case on shared GPU nodes), **omitting
`--gres=gpu`** lets you submit a CPU-only job there. Many clusters allow this;
your job lands on the GPU node's spare CPUs without claiming a GPU. Useful
escape valve when CPU partitions are fragmented.

## Job lifecycle: states the MCP tool must track

The states reported by `squeue` / `sacct` (`%T` / `State` field):

### In-flight (transient — keep polling)

| Code | Name | Meaning |
|---|---|---|
| `PD` | PENDING | In the queue, waiting for resources or priority. `Reason` field tells you why (`Priority`, `Resources`, `Dependency`, `QOSGrpCpuLimit`, …) |
| `CF` | CONFIGURING | Nodes are being prepared; rare to observe |
| `R` | RUNNING | Actively executing |
| `S` | SUSPENDED | Paused by admin or preemption; usually resumes |
| `CG` | COMPLETING | Allocation being torn down after the script exited; transient (<1 min) |

### Terminal — success

| Code | Name | Meaning |
|---|---|---|
| `CD` | COMPLETED | Script exited 0 within its walltime |

A job in `COMPLETED` state with `ExitCode 0:0` is the unambiguous success
condition. Anything else needs interpretation.

### Terminal — failure or interruption

| Code | Name | Meaning | Retry sensible? |
|---|---|---|---|
| `F` | FAILED | Non-zero exit code from your script | NO — fix the script |
| `TO` | TIMEOUT | Hit the `--time` walltime limit | maybe (with longer `--time` and/or `--partition`) |
| `NF` | NODE_FAIL | A node went down mid-execution | YES — transient hardware fault |
| `BF` | BOOT_FAIL | Node failed to boot for the job | YES — transient |
| `OOM` | OUT_OF_MEMORY | Hit the memory limit | maybe (with larger `--mem`) |
| `DL` | DEADLINE | Job deadline reached before start | YES — typically a queue-pressure issue |
| `PR` | PREEMPTED | Higher-priority job evicted yours | YES (typically auto-requeues) |
| `RV` | REVOKED | Reservation removed | NO — admin intervention needed |
| `CA` | CANCELLED | You or an admin ran `scancel` | NO — intentional |

**Heuristic for MCP-tool retry logic:**

- `CD 0:0` → success, done
- `F` / `CA` → permanent failure; do not retry; surface to the agent for analysis
- `NF` / `BF` / `PR` → transient infrastructure; retry once with the same parameters
- `TO` / `OOM` → resource undershoot; surface to the agent with the original request so it can decide whether to widen
- `PD` / `CF` / `R` / `S` / `CG` → keep polling

## `sacct`: the source of truth for historical jobs

`squeue` only shows live jobs. For anything that has left the queue, use
`sacct`. Field reliability across Slurm versions:

| Field | Stable? | Notes |
|---|---|---|
| `JobID` | yes | Format: `<id>` for head job, `<id>.<step>` for steps |
| `State` | yes | Code values per the lifecycle table above |
| `ExitCode` | yes | `<exit>:<signal>`; `0:0` means clean exit |
| `Elapsed` | yes | `[DD-]HH:MM:SS` |
| `Start` / `End` | yes | ISO-ish, but values become `Unknown` for jobs that never started |
| `NodeList` | yes | Compact hostname list (`compute[01-04]`) |
| `Reason` | **NO** | Format varies between versions; useful for humans, not for parsing |
| `AllocCPUs`, `AllocTRES` | mostly | TRES syntax can change; use `--format` explicitly |

**Use `-P` for pipe-delimited output** when parsing programmatically — the
default columnar format right-pads and is hard to split on:

```bash
sacct -j <jobid> --format=JobID,State,ExitCode,Elapsed,Start,End -X -n -P
#  -X  show only the head job (skip job steps)
#  -n  suppress the column header
#  -P  pipe-delimited (parseable)
```

**Use `-S` to bound the start time** when scanning history — without it,
`sacct` queries can become very slow on busy clusters:

```bash
sacct -u $USER -S now-1day -X --format=JobID,State,ExitCode,Elapsed
```

## `squeue`: the live view

```bash
squeue -u $USER                  # your jobs (running + pending)
squeue -j <jobid>                # one specific job
squeue -u $USER -t PD            # only pending; useful to see queue-wait reasons
squeue -j <jobid> -o '%T %r %S'  # state, reason, estimated-start-time
```

The `Reason` (`%r`) column is informational: `Priority` means waiting your
turn; `Resources` means no node has the shape you asked for; `QOSGrpCpuLimit`
means your QOS denied submission; `Dependency` means a parent job hasn't
finished.

## `scancel`: cleanup

```bash
scancel <jobid>             # cancel one job
scancel -u $USER            # cancel all your jobs (be careful)
scancel <jobid>_<index>     # cancel one task of a job array
```

Cancellation transitions the job to state `CA` (Cancelled). A killed live job
with exit-code `0:0` is normal — Slurm reports the cancellation in `State`,
not via `ExitCode`.

## Common Mistakes

| Mistake | Correct |
|---|---|
| Submitting without `--account` / `--qos` (gets `users/suspended` defaults) | Always pass both explicitly; discover with `sacctmgr show association where user=$USER` |
| `--ntasks=N` when you wanted N threads | Use `--cpus-per-task=N --ntasks=1` for thread-parallel; `--ntasks=N` only for MPI / multiple processes |
| Asking for more cores per node than the node has | Check `sinfo -p <partition> -N -o "%N %c %m"` for per-node CPU count |
| Submitting a job longer than the partition's walltime | Read `MaxTime` from `scontrol show partition <name>`; route to a longer-walltime partition |
| Loading modules without first purging | `module purge` before any `module load` — otherwise cluster default modules pollute the env |
| Parsing `sacct`'s default columnar output | Use `--format=… -P -n` for pipe-delimited, header-free output |
| Relying on `Reason` field syntax | Treat it as human-readable only; for retry logic switch on `State` |
| Assuming `ExitCode 0:0` from a cancelled job means success | Check `State` first; cancelled jobs report 0:0 even though they didn't finish |
| Asking `--gres=gpu:N` on a CPU partition | Only request GPUs on GPU partitions; mismatched requests stay PENDING with `Reason=PartitionConfig` |
| Polling `squeue` after the job left the queue (returns empty, looks like "still running") | Switch to `sacct` once the job is gone from `squeue`; cluster history is queried there |

## Heuristics for resource requests

When the workload's profile is unknown (Magnolia submitting a tool it's never
profiled), use these conservative defaults:

| Workload class | `--cpus-per-task` | `--time` | `--mem` (if required) |
|---|---|---|---|
| Quick test / smoke | 4 | 00:30:00 | 4G |
| Single-protein / single-ligand calc | 4–8 | 04:00:00 | 8G |
| Small docking screen (≤100 poses) | 16 | 24:00:00 | 32G |
| Large screen / MD simulation | 32 | per-partition cap | 64G |

Always cap `--time` to the partition's `MaxTime` — Slurm refuses jobs whose
requested time exceeds the partition cap.

## When this rule is wrong

- Slurm major version bumps to 26.x and renames state codes — re-check this
  rule's State table.
- The cluster uses a fork of Slurm (e.g. some sites add custom states like
  `DEADLINE_QUEUED`) — fold the cluster's specifics into `rules/hpc_<cluster>.md`.
- New Slurm features (heterogeneous jobs, federation, GPU sharding) — these
  aren't covered here; consult the upstream docs at https://slurm.schedmd.com/.
