---
name: hpc_azzurra
description: Access procedure and operating conventions for the Azzurra HPC cluster at Université Côte d'Azur.
version: 1.2
last_verified: 2026-05-28
tags: [hpc, slurm, azzurra, vpn, university-cote-dazur]
---

# Azzurra HPC Rules

## Overview

Azzurra is the HPC cluster operated by Université Côte d'Azur
(https://calculs.univ-cotedazur.fr/). Magnolia uses it as the primary remote
compute target for jobs too large or too long for the local machine.

Three layers stack together to reach it — a VPN, an SSH connection over the
VPN, and the Slurm scheduler on the cluster itself. Each layer has gotchas
that cost real time to rediscover; this rule documents them so they don't
have to be relearned.

## Cluster Facts

| Fact | Value |
|---|---|
| Login node (public) | `login-hpc.univ-cotedazur.fr` → `134.59.101.97` |
| Internal hostname | `login-hpc.cluster.local` |
| OS / kernel family | RHEL 9.5-derived, `5.14.0-503.x.el9_5` kernel |
| Scheduler | **Slurm 24.11.5** (`sbatch`, `squeue`, `sinfo`, `sacct` in `/usr/bin`) |
| Modules | **Lmod 8.7.53** (Lua-based; `module load`, `module avail`, `module spider`) |
| `$HOME` | `/home/<user>` on 107T NFS (`192.168.204.253:/mnt/nss/home`) |
| Scratch / shared writable | `/workspace` (**BeeGFS**, 428T parallel filesystem — small-file-friendly, prefer over `$HOME` for conda envs and run dirs), `/tmp` (local) |
| Partitions (CPU) | `cpucourt` (default, 3 d), `cpulong` (15 d), `cpucourt-k` (3 d), `cpulong-k` (15 d) |
| Partitions (GPU) | `gpu` (1.5 d), `gpu-icn` (1.5 d), `gpu-icn-prio` (21 d), `res-gpu` (1.5 d) — six nodes `gpu[01-06]` |
| Partitions (other) | `smp` / `smp-rsc` / `smp-instar` (large-memory); `amd-k` / `amdcourt-k` (AMD-arch nodes `compute[33-48]`); `visu` (interactive viz); `benchmark`, `reservation` (special-purpose) |
| Accounts / QOS gotcha | Default account `users` has QOS `suspended` — all `sbatch` submissions fail. Pass `--account=<your-account> --qos=<your-qos>` explicitly. Check yours with `sacctmgr show association where user=$USER format=Account,QOS`. |

### Pre-provided modules of interest

These modules are provided system-wide on Azzurra (probed 2026-05-28). Use
`module avail` for the current full list; this is the curated subset Magnolia
relies on:

- **Compilers:** `intel/2025.2.0` (default), `intel/{2023.2.1,2024.0.0,2025,2025.0.4,2025.1.1}`, `intel/2020-cluster-xe`, `gcc/12.2.0`. Intel oneAPI bundles MKL.
- **Build tools:** `cmake/4.1.2`, `cmake/intel/4.1.1`.
- **Python:** `python/3.13.2` (default), `python/3.8.1`.
- **MPI:** `openmpi/4.1.7` (default), `openmpi/5.0.8-cuda12.8`.
- **Conda:** `miniconda/25.1.1` — used as the base for user-local conda envs (Magnolia installs HADDOCK3 into a path-based env under `/workspace/<user>/envs/`; no need to bootstrap miniforge yourself).
- **Pre-built scientific software:** `gromacs/2024.1`, `gromacs/2025.1`.

User-local modulefiles go under `~/modulefiles/<tool>/<version>.lua`; add
`module use $HOME/modulefiles` to `~/.bashrc` to make them discoverable.

### Magnolia-installed modules (user-local)

These live under `~/modulefiles/` on the cluster and are discoverable
because `module use $HOME/modulefiles` is in `~/.bashrc`. Installed during
Sub-project A (2026-05-28); see `docs/azzurra-setup.md` §2-3 for the
install recipe.

- **`xtb/local`** → xtb 6.7.1 static binary at `~/software/xtb/6.7.1/`.
  Single-core, MKL bundled. Load with `module load xtb/local` →
  `xtb --version` should print the standard banner.
- **`haddock3/local`** → HADDOCK3 2026.5.0 in a conda env at
  `/workspace/tjiang/envs/magnolia/`, with the HADDOCK-patched CNSsolve
  1.3 UU binary copied in at
  `lib/python3.11/site-packages/haddock/cns/bin/x86_64-linux.bin`. Load
  with `module load haddock3/local` (the modulefile activates the conda
  env and exposes `haddock3` on PATH).

When Magnolia submits via `submit_job(scheduler="ssh-slurm", tool="xtb"|"haddock3", ...)`,
the sbatch template emits `module load <tool>/local` automatically — see
`compchem-tools/tools/ssh_slurm.py:_write_sbatch_script`. You only call
`module load` manually when running interactively on the login node.

**If you need anything else** (e.g. ORCA, Gaussian, additional Gromacs
build): check `module avail` on the cluster first; if absent, install
user-local under `~/software/<tool>/<ver>/` + a modulefile at
`~/modulefiles/<tool>/<ver>.lua`, then add a bullet to this list.

Public key registration is **admin-managed** — no self-service portal. New users
email their SSH public key to the HPC admins (contacts at the bottom of
`https://calculs.univ-cotedazur.fr/?page_id=550&lang=en`).

## Access: VPN Required

The login node is firewalled. SSH from the public internet times out silently.
Two ways through:

1. **University VPN** (`open.unice.fr`, Cisco AnyConnect protocol) — current default.
2. **SSH bastion** (`bastionXX.unice.fr`, on request) — public-internet reachable,
   no VPN needed. Prefer this if granted.

**TL;DR for the VPN path:** run `hpc_tunnel.sh` (on PATH; idempotent — brings
the SOCKS5 tunnel up on `localhost:1080` if it isn't already, exits 0 if it
is). Always try this *first*. The detail below explains *why* it does what it
does; you only need it for troubleshooting. The full script contract is
documented at the end of this file under "Tunnel script".

### VPN credentials — the critical gotcha

VPN username **must** be `<unica-login>@hpc`, not `<unica-login>` alone.
The `@hpc` suffix selects the HPC-routing realm. Without it the VPN connects
but doesn't route to the HPC subnet — SSH to the login node times out with
no useful error.

### Recommended client: openconnect + ocproxy (split-tunneling)

The official Cisco Secure Client is **full-tunnel**: once connected, all
external traffic (including the GLM provider Magnolia relies on) is forced
through the university gateway and dropped. This breaks Magnolia agents during
cluster work.

`openconnect` + `ocproxy` solves it by exposing the VPN as a SOCKS5 proxy on
`localhost:1080` instead of touching the kernel routing table. Only traffic
explicitly sent through the proxy traverses the VPN; everything else stays on
normal internet, so the agent's provider connection survives.

**One-time install (Debian/Ubuntu, e.g. WSL2):**

```
sudo apt-get install -y openconnect ocproxy
```

**Sudoers rule** (`/etc/sudoers.d/openconnect`, root-owned, mode 0440):

```
# Required for the unattended tunnel-start pattern.
<your-user> ALL=(root) NOPASSWD: /usr/sbin/openconnect

# Required for tunnel teardown (signal openconnect, which runs as root).
# Glob restricts pkill to invocations targeting openconnect-related processes.
<your-user> ALL=(root) NOPASSWD: /usr/bin/pkill -f openconnect*
```

The `pkill` line is what lets `hpc_tunnel.sh` (or operator scripts) tear the tunnel down without prompting. Don't broaden the pkill glob — `pkill -f openconnect*` lets you target only processes whose command line begins with `openconnect`; anything wider gives root-level kill rights over arbitrary processes.

**Watch out:** an `hpc_tunnel.sh`-style script must NOT test `sudo -n true` to validate the rule — `true` isn't in this scope so the check produces false-negatives. Use `sudo -n -l /usr/sbin/openconnect` to test the actual permission. See commit `f383fb3` for the fix.

**Start the tunnel:** run `hpc_tunnel.sh` (on PATH; idempotent). Full
contract documented under "Tunnel script: `hpc_tunnel.sh`" at the end of
this file. **This is the only command an operator or agent should issue
to start the tunnel.** Everything below in this subsection explains
what the script does internally; it is reference material, not a
procedure.

<details>
<summary>Manual command the script wraps (reference only — do NOT use directly)</summary>

```
sudo openconnect \
    --csd-wrapper=/usr/libexec/openconnect/csd-post.sh \
    --user='<unica-login>@hpc' \
    --script-tun \
    --script 'ocproxy -D 1080' \
    open.unice.fr
```

Auth flow seen as of 2026-05-18: a `Password:` prompt for the UniCA password.
No 2FA observed for the `@hpc` realm. If 2FA is later enforced, the unattended
pattern breaks — design for "human establishes tunnel once per day"
instead.

</details>

**Stop the tunnel:** Ctrl-C the openconnect process, or for backgrounded
invocations: `sudo -n pkill -f 'openconnect.*open.unice'` (covered by the
NOPASSWD sudoers rule above; pattern is specific enough not to false-match
shell argv strings containing "openconnect"). Session has an idle timeout
of 30 min and a hard timeout of ~14 h regardless.

### Why each openconnect flag is required

| Flag | Reason |
|---|---|
| `--csd-wrapper=/usr/libexec/openconnect/csd-post.sh` | UniCA's ASA demands a Cisco HostScan/CSD posture check. Without it, openconnect 404s on `/CACHE/sdesktop/install/binaries/sfinst` and aborts. The bundled script fakes a `TOKEN_SUCCESS` response. |
| `--user='<login>@hpc'` | Selects the HPC routing realm. |
| `--script-tun --script 'ocproxy -D 1080'` | Runs lwIP in userland (`--script-tun`) and uses ocproxy as the data handler instead of a kernel TUN device. **Use `-D` (SOCKS5 dynamic), not `-L`** — `-L` is for static port forwards and is rejected with "Invalid port forward specifier". |

## SSH / SCP / rsync through the SOCKS proxy

With the tunnel up, standard tools reach the cluster by being told to use the
SOCKS proxy. Persist this in `~/.ssh/config`:

```
Host azzurra
    HostName login-hpc.univ-cotedazur.fr
    User <unica-login>
    IdentityFile ~/.ssh/<your-key>
    ProxyCommand nc -X 5 -x localhost:1080 %h %p
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

The two `ServerAlive*` lines defend against a different layer than the
VPN-server-side timeouts mentioned above. **Two timeouts apply, independently:**

- **VPN-gateway-side** (`open.unice.fr`): 30 min idle, ~14 h hard. Tearing
  this down kills the SOCKS proxy and every SSH session through it. Mitigated
  by re-running `openconnect`.
- **TCP-keepalive / NAT-side**: an SSH session through SOCKS that goes
  quiet for a few minutes can be dropped by an intermediate NAT or by the
  cluster's idle reaper, even with the VPN still up. `ServerAliveInterval 60
  ServerAliveCountMax 3` makes the SSH client send a keepalive every minute
  and tolerates three misses — keeps long `ssh azzurra '... sbatch ...; sacct
  ...'` sessions alive.

Then every tool collapses to the bare alias:

```
ssh azzurra                                  # interactive shell (debugging)
ssh azzurra 'sinfo -s'                       # cluster state (read-only)
ssh azzurra 'sacct -j <jobid> -X -P -n'      # post-mortem inspection
scp -r data/ azzurra:/workspace/<user>/run/  # ad-hoc input staging (one-offs only)
rsync -avzP azzurra:/workspace/<user>/runs/<id>/ ./  # ad-hoc result pull
```

**Notably absent:** `ssh azzurra 'sbatch ...'`. Job submission goes through
the `submit_job` MCP tool — see §"Submitting jobs — the only supported
path" under "Slurm on Azzurra" below. The shell aliases here are for
read-only inspection and one-off file moves, not for spawning computations.

`paramiko` and `fabric` honor `~/.ssh/config`, so Magnolia's Python code reaches
the cluster with the bare host alias as well — no special proxy plumbing needed
in code.

## Password Management

Three secrets are involved. Handle each properly:

| Secret | Recommended storage |
|---|---|
| `sudo` for openconnect | NOPASSWD sudoers rule scoped to `/usr/sbin/openconnect` only (see above) |
| UniCA VPN password | `pass` (passwordstore.org) + GPG agent; retrieve with `pass show univ-cotedazur/vpn` and pipe via `--passwd-on-stdin` |
| SSH key passphrase | `ssh-agent` + `ssh-add` once per shell session |

Don't put any of these in environment variables, command-line args (visible in
`/proc`), or plaintext files. The `pass` + GPG-agent pattern is the standard for
the UniCA password.

Unattended-tunnel invocation — wrapped by `hpc_tunnel.sh`. **Always call
`hpc_tunnel.sh`, not this command directly.** The expansion is shown only
to explain how `pass` feeds the password into openconnect without it ever
hitting `/proc` argv or a plaintext file:

```
pass show univ-cotedazur/vpn | sudo openconnect --passwd-on-stdin \
    --csd-wrapper=/usr/libexec/openconnect/csd-post.sh \
    --user='<unica-login>@hpc' \
    --script-tun --script 'ocproxy -D 1080' \
    open.unice.fr
```

## Slurm on Azzurra

Slurm semantics (sbatch directives, state machine, sacct parsing, retry
heuristics, `--cpus-per-task` vs `--ntasks`, `--gres` for GPUs) are documented
once in **`rules/slurm.md`**. This section covers only what's Azzurra-specific.

### Submitting jobs — the only supported path

**Use the `submit_job` MCP tool with `scheduler="ssh-slurm"`. Do NOT issue
`ssh azzurra 'sbatch ...'` from `compchem-tools_run_shell` directly.** Raw-shell submission
*works* in the sense that sbatch will accept the job, but it bypasses every
lifecycle guarantee Magnolia provides:

| What you skip by going around `submit_job` | Consequence |
|---|---|
| Local `runs/<id>.yaml` record (writeahead → submitted → ... → fetched) | Magnolia has no idea this job exists; the poller can't monitor it; no cross-session memory |
| `.magnolia/manifest.json` written into the remote run dir | The run isn't self-describing on the cluster; you can't reconstruct it later from cluster scratch alone |
| `_ensure_tunnel()` auto-call | You're responsible for `hpc_tunnel.sh` yourself; easy to forget |
| `module load <tool>/local` auto-injection into the sbatch script | You must remember to load xtb/haddock3 modules in your script |
| Auto-polling, auto-fetch, auto-assess on terminal state | You must manually `ssh azzurra sacct ...`, `rsync ...`, `post_run_assess(...)` for every run |
| Failure capture (log-tail → staging memory entry) | When a job fails, you lose the cross-session learning loop |

**Canonical submission (haddock3 example):**

```python
submit_job(
    scheduler="ssh-slurm",
    tool="haddock3",
    command="haddock3 run.cfg",
    working_dir="/local/path/to/run_inputs",  # rsynced to cluster
    project_dir="projects/<your-project>",
    job_name="docking_wt",
    ncores=8, memory="32GB", time_limit="04:00:00",
    # account/qos/partition default to the Azzurra cluster config; override if needed
)
```

After this returns `{success: True, job_id: ..., run_id: ...}`, the poller
(running every `MAGNOLIA_POLL_INTERVAL_MIN` minutes, default 5) takes over:
polls sacct, auto-fetches when terminal, runs `assess_and_record` on
success or `capture_failure` on failure. You walk away.

**For on-demand status checks** between polls, use the `poll_jobs` MCP tool
(triggers an immediate sweep). For one-off queries (`sinfo`, `module avail`,
ad-hoc diagnostics), `compchem-tools_run_shell("ssh azzurra ...")` is fine — but for
*submitting computations*, only `submit_job` is supported.

### The account / QOS gotcha (real submission failures)

The default account for new users is `users` with QOS `suspended` — every
submission with these defaults fails with `QOSGrpCpuLimit`. You **must** pass
`--account` explicitly to avoid the default:

```bash
#SBATCH --account=spectrometry         # whatever account you belong to
```

**Do NOT pass `--qos` explicitly** — on Azzurra the `spectrometry` account works when
Slurm auto-assigns QOS from the association, but passing `--qos=qos_spectrometry`
triggers `QOSGrpCpuLimit` (verified 2026-05-30, job ID 11335011). Let the
scheduler infer QOS from the account.

Discover your account with:
```bash
ssh azzurra 'sacctmgr show association where user=$USER format=Account,QOS'
```

### Partition cheat-sheet (Azzurra-specific names)

Choose by expected wall time; memory and CPU shape are secondary.

| Wall time | CPU partition | GPU partition | Large-memory |
|---|---|---|---|
| ≤ 3 days | `cpucourt` (default), `cpucourt-k` | `gpu` (1.5 d), `gpu-icn` (1.5 d) | — |
| 3–15 days | `cpulong`, `cpulong-k` | `gpu-icn-prio` (21 d) | `smp-rsc` |
| >15 days | — | — | `smp-instar` (31 d), `smp` (18 d) |

GPU types: `gpu01-02` = V100, `gpu03` = A100, `gpu04-06` = H100. Use
`--gres=gpu:<type>:N` to pin the type, or `--gres=gpu:N` for any.

### CPU-only on GPU partitions (escape valve for cpucourt fragmentation)

`cpucourt` can have many CPUs idle in aggregate (e.g. 12 across 82 nodes) yet
no single node with enough contiguous free cores for `--cpus-per-task=4`. The
`gpu` partition typically has 50-150 CPUs spread across 6 nodes — submit there
**without** `--gres=gpu` and your job lands on a GPU node's spare CPUs.

Confirmed working on `spectrometry` (which has `gpu` partition access per
`AllowAccounts`). Check your account's gpu-partition access with
`scontrol show partition gpu | grep AllowAccounts`.

### Minimal Azzurra sbatch template

```bash
#!/bin/bash
#SBATCH --job-name=<name>
#SBATCH --account=spectrometry         # your account
#SBATCH --partition=cpucourt           # see cheat-sheet above
#SBATCH --time=24:00:00                # walltime cap (must fit partition's max)
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

module purge
module use $HOME/modulefiles           # discover user-local modulefiles
module load <tool-stack>
<your-workload>
```

### Manual cluster commands (read-only / troubleshooting only)

For **read-only inspection** and one-off probing, these are the canonical
shell commands. **They do not include submitting jobs** — that goes through
`submit_job(scheduler="ssh-slurm", ...)` (see "Submitting jobs — the only
supported path" above).

```bash
hpc_tunnel.sh                                        # idempotent, brings VPN up
ssh azzurra 'sinfo -s'                               # partition + node-state summary
ssh azzurra 'squeue -u $USER'                        # your live queue
ssh azzurra 'sacct -j <jobid> -X -P -n --format=…'   # post-mortem after a submit_job run
ssh azzurra 'module avail xtb haddock3'              # is software X installed?
```

For staging files or pulling results **outside the `submit_job` flow** (e.g.
recovering an orphan run that someone submitted via raw shell before the
rule update), use:

```bash
scp job.slurm azzurra:/workspace/$USER/                                   # ad-hoc input
rsync -avzP azzurra:/workspace/$USER/runs/<run-id>/ ./runs/<run-id>/      # ad-hoc pull
```

These should be rare — `submit_job` writes inputs to the cluster and
`fetch_job_results` (or the auto-poller) pulls outputs back, both
automatically.

Aliases `azzurra-status` (queue + recent history) and `azzurra` (interactive
ssh, with tunnel auto-start) are documented in `docs/azzurra-setup.md`.

## Common Mistakes (Azzurra-specific)

For Slurm-generic mistakes (account/QOS confusion, `--cpus-per-task` vs `--ntasks`, `--gres` semantics, etc.) see `rules/slurm.md`. These are Azzurra-specific.

| Mistake | Correct |
|---|---|
| Cisco Secure Client on Windows (full-tunnel) | `openconnect` + `ocproxy` from WSL/Linux — preserves provider access |
| VPN username = `<login>` | VPN username = `<login>@hpc` |
| `ocproxy -L 1080` | `ocproxy -D 1080` (SOCKS5 dynamic forwarding) |
| `openconnect` without `--csd-wrapper` | Hostscan posture check 404s and auth aborts; add `--csd-wrapper=/usr/libexec/openconnect/csd-post.sh` |
| Direct SSH to the login node from public internet | Always via the SOCKS proxy (or bastion if granted) |
| Storing UniCA password in env var / plaintext file | Use `pass` + GPG; pipe via `--passwd-on-stdin` |
| Submitting without `--account=spectrometry` | Default `users/suspended` rejects every submission; pass `--account` explicitly. Do NOT pass `--qos` — auto-assigned QOS works; explicit `--qos=qos_spectrometry` triggers `QOSGrpCpuLimit` |
| Submitting a 4-day job to `cpucourt` | `cpucourt` caps at 3 days; use `cpulong` (15 d) or one of the `*-k` variants |
| Heavy I/O on `/home` | Stage scratch work to `/workspace` (BeeGFS); `/home` is NFS and shared with many users |
| Conda env on `/home` | Install conda envs under `/workspace/<user>/envs/`; NFS small-file pressure on `/home` is the documented worst-case |

## Tunnel script: `hpc_tunnel.sh` (canonical entry point)

**Live as of Sub-project A (2026-05-28).** Located at
`opencode_cc_mem/softwares/bin/hpc_tunnel.sh`, on PATH. **Always call this
first when the tunnel might be down — do not reinvent the manual
`sudo openconnect …` command shown earlier; that command is documented
only for understanding what this script does and for one-off
troubleshooting.**

Idempotent contract (safe to call before every cluster operation):

1. If `openconnect` against `open.unice.fr` is already running, exit 0.
2. Otherwise verify port 1080 is listening locally; exit 0 if yes.
3. Otherwise pipe `pass show univ-cotedazur/vpn` into `sudo openconnect --passwd-on-stdin …` and background it.
4. Poll `ss -ltn 'sport = :1080'` until the port appears (timeout 30s) before returning success.
5. Non-zero exit on any failure with a clear message; Magnolia code surfaces it via `memory_record_learning(entry_type="error_resolution", ...)`.

**Log file:** `~/.cache/magnolia/hpc-tunnel.log`. When something fails,
look here. The common idle-disconnect signature is:

```
Received server disconnect: b0 'Idle Timeout'
Send BYE packet: Server request
Session terminated by server; exiting.
ocproxy: VPN connection has terminated
```

— just re-run `hpc_tunnel.sh`; UniCA dropped the previous session for
inactivity and nothing else is wrong.

**Don't trust `pgrep -af openconnect` alone to decide "is the tunnel
up?"** — it false-positives on wrapper command lines whose argv strings
contain `openconnect` (including the very bash command you're using to
probe). Either parse `pgrep` output more carefully or just call
`hpc_tunnel.sh` (the script handles this itself; see commit `f383fb3`).

## When This Rule Is Wrong

- Admins migrate the scheduler, change partition names, or move the login
  hostname → re-probe and update.
- 2FA is enforced on the `@hpc` realm → the unattended pattern breaks;
  switch to "human-establishes-tunnel-once-per-day" or push the agent onto
  the cluster itself.
- Bastion access is granted → prefer it; no VPN at all is simpler.
- The `csd-post.sh` wrapper stops satisfying the posture check after an ASA
  upgrade → try `--useragent='AnyConnect Windows 4.10.06079'` or build a
  custom CSD wrapper that mimics the official client response.
