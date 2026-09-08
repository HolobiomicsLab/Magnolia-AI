# Cluster execution

Magnolia runs on your own machine; the cluster performs the computation. This document describes how
a site is declared, what governs submission, and how results find their way back into the notebook.

## The division of labour

The assistant, its notebook and its rules stay local. What travels to the cluster is a job: an input
set, a script, a resource request. What comes back is a result and a status, which are ingested into
the project so that the account of the campaign remains complete whether or not you were watching
when the job ended.

This is a deliberate limitation. Magnolia does not run on the login node, does not hold a session
open across a queue wait, and does not require anything to be installed on the cluster beyond the
scientific software you would have needed anyway.

## Declaring a cluster

**Adding a cluster is a configuration change, not a code change.** The generic backend knows how to
drive *a* Slurm cluster over SSH; it must not know which one. Everything specific to a site is data.

Three sources are consulted, later ones overriding earlier ones key by key:

1. `clusters.yaml` packaged beside the backend — site facts for clusters the project has been used
   on. Public, committed, and containing no per-person value.
2. `~/.config/magnolia/clusters.yaml` — your own overrides and your private sites.
3. `$MAGNOLIA_CLUSTERS_FILE` — an explicit path, for tests and one-off runs.

The layering exists so that a site profile can ship a partition while the person running it supplies
their own account. **Your username, your personal account and anything else true of you rather than
of the machine belong in the second file**, which lives outside the repository and is never
committed.

The minimum profile is two lines:

```yaml
clusters:
  mycluster:
    ssh_host: mycluster-login          # an alias from your ~/.ssh/config
    scratch_root: /scratch/{user}/magnolia
```

Every other key defaults to *this site does not have one*, and the corresponding step is skipped: no
`tunnel_script` means no tunnel, an empty `default_account` means no `#SBATCH --account` and Slurm
picks your default association. A bare university cluster needs none of them and must not be made to
pretend otherwise.

| Key | Meaning when set |
|---|---|
| `ssh_host` | SSH alias for the login node, resolved from your `~/.ssh/config` |
| `scratch_root` | Where work is staged; `{user}` is substituted |
| `default_user`, `default_account`, `default_qos`, `default_partition` | Submission defaults |
| `tunnel_script` | A script run before connecting, for sites behind a VPN |
| `modulefiles_use` | A path added with `module use` |
| `requires_control_master` | The site enforces interactive authentication; see below |
| `default` | Marks the profile chosen when the caller names none |

**Which cluster, when you do not say.** `$MAGNOLIA_CLUSTER`, failing that the profile marked
`default: true`, failing that the only profile configured. With several profiles and no default,
resolution fails and names them — guessing would submit someone's job, and someone's compute
budget, to the wrong machine.

**Two-factor authentication.** Where a site enforces it on the login node, non-interactive SSH
cannot succeed on its own; it can only travel over a connection a human has already opened
(`requires_control_master: true`). Open your usual session first, and Magnolia's submissions will
share it.

## What governs a submission

Two rules are read at the start of every session and are not optional.

**Every computation writes to `runs/YYYY-MM-DD_name/`**
([`job_execution.md`](../opencode_cc_mem/rules/job_execution.md)). A single command, a pipeline or a
Slurm job all obey it. The convention is what makes a campaign reconstructible six months later, and
what allows results to be attached to the right record automatically.

**Inputs are verified before submission**
([`prejob_check.md`](../opencode_cc_mem/rules/prejob_check.md)). A job that fails ninety minutes in
because residue numbering disagrees between the configuration and the structure has wasted an
allocation, a place in the queue and your attention. The check is mandatory before any `submit_job`.

Cluster-agnostic Slurm semantics — directives, the state machine, `sacct` behaviour, the usual
traps — are in [`slurm.md`](../opencode_cc_mem/rules/slurm.md). Site-specific human procedure
belongs in a per-cluster rule written from
[`hpc_cluster.template.md`](../opencode_cc_mem/rules/hpc_cluster.template.md); such files tend to
accumulate account names and login details and are therefore kept out of the repository.

## The job life-cycle

| Stage | Tool | Note |
|---|---|---|
| Prepare | `haddock3_run`, `gromacs_setup`, `orca_setup`, … | Written into a self-contained run directory |
| Verify | `stage_gate`, `validate_structure` | The pre-job checklist |
| Submit | `submit_job` | Resolves the cluster, stages inputs, submits |
| Watch | `check_job`, `poll_jobs`, `run_progress` | Polling is done in the background; you need not stay |
| Retrieve | `fetch_job_results` | Brings results back into the project |
| Record | `magnolia-memory sync-queue` | Ingests queued events into the notebook |
| Abandon | `cancel_job` | |

Results that arrive while no session is open are queued and ingested by the next session, or by
`magnolia-selfreflex` if you run it from cron — see [memory.md](memory.md).

## A representative session

> *"Submit the four HADDOCK3 runs prepared in `runs/2026-09-05_series/`. Before submitting, verify
> that the residue numbering in each restraints file matches its structure. Use the group account,
> tell me the estimated queue time, and poll them; I will be away this afternoon."*

and, the following morning:

> *"What is the state of the four jobs? Fetch the results of any that completed and summarise the
> scores against our June baseline."*

## Related

- [`hpc-session`](https://github.com/HolobiomicsLab/hpc-session) — the laboratory's standalone tool
  for a single authenticated window onto a Slurm cluster, including VPN and two-factor
  authentication.
- [tools.md](tools.md) — the full signature of each job tool.
