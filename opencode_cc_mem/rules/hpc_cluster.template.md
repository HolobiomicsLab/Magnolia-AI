---
name: hpc_cluster_template
description: Starting point for your private cluster skill. Copy to ~/.config/opencode/skills/hpc-<cluster>/SKILL.md, fill in from the discovery commands, and register the machine-readable half in clusters.yaml.
version: 1.1
last_verified: 2026-09-16
tags: [slurm, hpc, template]
---

# `<cluster>` — your cluster skill (template)

The `slurm` skill covers what is true of every Slurm cluster. Your cluster
skill covers what is true only of yours. Copy this file to
`~/.config/opencode/skills/hpc-<cluster>/SKILL.md` and fill it in.

Your filled-in skill lives **outside the repository**, in your opencode
config directory: it cannot be committed or shared by accident. The template
is committed so the pointer in the `slurm` skill always resolves.

## Register your cluster facts (so promotion leaves them alone)

List regexes of your cluster's private strings — hostnames, account names,
VPN addresses — under `cluster_facts:` in the skill's frontmatter. Magnolia's
promotion step reads them and keeps lessons that match out of all shared
files (see `magnolia-destinations.yaml`):

```yaml
---
name: hpc-<cluster>
description: "..."
metadata:
  version: "1.0"
  last_verified: "2026-09-16"
cluster_facts:
  - <cluster>
  - <login-hostname regex>
  - --account[= ]<your-account>
---
```

## Two halves, two places

The agent reads prose; the submission backend reads config. Keep both, and keep
them agreeing:

| Half | Lives in | Read by |
|---|---|---|
| Prose — how to get on, what the queues are for, what breaks | `~/.config/opencode/skills/hpc-<cluster>/SKILL.md` (this file, filled in) | the agent |
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

Fill in from the discovery commands in the `slurm` skill:

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

The reason this skill is worth keeping. One line each: the symptom you saw, and
what it actually was.
