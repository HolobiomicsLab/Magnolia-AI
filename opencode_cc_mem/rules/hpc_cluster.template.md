---
name: hpc_cluster_template
description: Starting point for a per-cluster rule. Copy to rules/hpc_<cluster>.md, fill in from the discovery commands, and register the machine-readable half in clusters.yaml.
version: 1.0
last_verified: 2026-09-03
tags: [slurm, hpc, template]
---

# `<cluster>` — site rule (template)

`slurm.md` covers what is true of every Slurm cluster. This file covers what is
true only of yours. Copy it to `rules/hpc_<cluster>.md` and fill it in.

Per-cluster rule files are gitignored: they end up holding account names, login
handles and paths that belong to one group at one site. The template is
committed so the pointer in `slurm.md` always resolves; your filled-in copy is
not.

## Two halves, two places

The agent reads prose; the submission backend reads config. Keep both, and keep
them agreeing:

| Half | Lives in | Read by |
|---|---|---|
| Prose — how to get on, what the queues are for, what breaks | `rules/hpc_<cluster>.md` (this file, filled in) | the agent |
| Values — ssh alias, scratch layout, account, partition, tunnel | `compchem_tools/tools/clusters.yaml`, or `~/.config/magnolia/clusters.yaml` | `ssh_slurm` |

The config half is the one that decides where a job actually lands. A cluster
that is described here but absent from `clusters.yaml` cannot be submitted to.

```yaml
# ~/.config/magnolia/clusters.yaml — overrides the packaged file key by key
clusters:
  <cluster>:
    ssh_host: <alias from your ~/.ssh/config>
    scratch_root: /scratch/{user}/magnolia   # {user} expands to your login
    default_account: ""                      # empty -> no #SBATCH --account
    default_qos: ""
    default_partition: ""
    tunnel_script: ""                        # empty -> no tunnel step
    modulefiles_use: ""                      # empty -> no `module use`
    requires_control_master: true            # false if ssh works unattended
```

With one cluster configured, `cluster` can be omitted everywhere. With several,
mark one `default: true` or set `$MAGNOLIA_CLUSTER` — otherwise submission
fails and names the candidates rather than guessing.

## Access

- **How you reach the login node.** Direct ssh, a VPN, a bastion? If a script
  must run first, name it in `tunnel_script` so every remote call runs it.
- **Whether ssh can authenticate unattended.** If the site enforces 2FA, ssh
  with `BatchMode=yes` can only work by riding a ControlMaster a human opened;
  keep `requires_control_master: true` and say here how to open one. If keys
  work on their own, set it to `false` and drop the ceremony.
- **Where scratch lives, and what gets purged when.**

## Your account, partition and QOS

Fill in from the discovery commands in `slurm.md`:

```bash
sacctmgr show association where user=$USER format=Account,QOS,Partition
sinfo -s                                     # partitions, walltime caps, node counts
scontrol show partition <name>               # MaxTime, AllowAccounts
```

Record the *values that work*, and any combination that is silently rejected —
that is the part nobody can rediscover from the man pages.

## Software

- Are modules site-standard, or do you need `module use <your dir>` first?
  The latter goes in `modulefiles_use`.
- Which tools are installed centrally, and which you had to build.

## Known failure modes

The reason this file is worth keeping. One line each: the symptom you saw, and
what it actually was.
