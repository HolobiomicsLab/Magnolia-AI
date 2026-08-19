# Azzurra Setup — As Executed

**Date:** 2026-05-28
**Executed by:** Tao Jiang (WSL2 / Debian-family)
**Branch:** `dev`
**Spec:** [docs/superpowers/specs/2026-05-28-hpc-azzurra-bootstrap-design.md](../docs/superpowers/specs/2026-05-28-hpc-azzurra-bootstrap-design.md) (gitignored)
**Plan:** [docs/superpowers/plans/2026-05-28-hpc-azzurra-bootstrap.md](../docs/superpowers/plans/2026-05-28-hpc-azzurra-bootstrap.md) (gitignored)

This file captures the procedure **as actually executed** in the 2026-05-28 session. For prescriptive conventions (what to follow on a fresh setup), see [opencode_cc_mem/rules/hpc_azzurra.md](../opencode_cc_mem/rules/hpc_azzurra.md). When the two diverge, the rule file is authoritative.

## 0. Prerequisites

- WSL2 (Debian-family) with sudo capability.
- UniCA account `tjiang@hpc` registered with HPC admins.
- SSH key `~/.ssh/id_tjiangazr` (RSA) — public key already emailed to admins and registered.
- `openconnect` + `ocproxy` already installed (`apt-get install openconnect ocproxy`).
- `~/.ssh/config` `Host azzurra` block already written (added earlier this session — see §1.4 for content).

## 1. Local channel setup

### 1.1 GPG + pass

The session's GPG state at start: `~/.gnupg/` did not exist.

Generated a primary key, then added an encryption subkey (the `--quick-generate-key` default creates only `[SC]` — sign+certify — without an encryption subkey, which `pass` requires):

```bash
gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "tjiang@magnolia-hpc-store" rsa4096 default never
# Output: gpg: key 8A5B495E2406ECC0 marked as ultimately trusted

gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-add-key C4722EF863B100960FFB9D6F8A5B495E2406ECC0 rsa4096 encr never
# Adds the [E] encryption subkey 2E6E0CB768819DF5
```

Empty passphrase is intentional: threat model is "WSL filesystem access," and a passphrase would defeat the unattended `hpc_tunnel.sh` flow downstream. If you want a passphrase, configure `gpg-agent` cache lifetime in `~/.gnupg/gpg-agent.conf`.

Installed `pass`:

```bash
sudo apt-get update && sudo apt-get install -y pass
KEYID=$(gpg --list-secret-keys --keyid-format=long --with-colons | awk -F: '/^sec/ {print $5; exit}')
pass init "$KEYID"
```

### 1.2 UniCA VPN password storage

```bash
pass insert univ-cotedazur/vpn
# (interactive; type UniCA password twice)
```

Verify the entry exists without revealing content:

```bash
pass show univ-cotedazur/vpn >/dev/null && echo "decrypts cleanly"
```

### 1.3 NOPASSWD sudoers rule for openconnect + pkill

```bash
sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/openconnect <<'EOF'
# Allow tjiang to run openconnect as root without a password.
# Required for the hpc_tunnel.sh unattended-startup pattern.
tjiang ALL=(root) NOPASSWD: /usr/sbin/openconnect

# Allow tearing down openconnect via pkill — needed for the tunnel
# integration test (M4.5) and idempotency test (M5.3). The argument glob
# matches any `pkill -f openconnect*` invocation; the actual pattern used
# by hpc_tunnel.sh teardown is 'openconnect.*open\.unice\.fr'.
tjiang ALL=(root) NOPASSWD: /usr/bin/pkill -f openconnect*
EOF

sudo visudo -c -f /etc/sudoers.d/openconnect
# Expected: /etc/sudoers.d/openconnect: parsed OK
```

This grants password-less sudo **only for `/usr/sbin/openconnect`** and **`/usr/bin/pkill -f openconnect*`** — every other `sudo <anything>` still prompts. Arguments aren't restricted on openconnect (tight arg-matching breaks on version bumps); pkill is restricted to invocations targeting openconnect-related processes only.

**Gotcha discovered during M4.5:** the original `hpc_tunnel.sh` check `sudo -n true` produced false-negatives on this minimal NOPASSWD scope (true isn't in the allowlist). Fixed in commit `f383fb3` by switching the check to `sudo -n -l /usr/sbin/openconnect`, which tests the actual permission we need.

### 1.4 SSH config

Already in place at `~/.ssh/config` from the earlier session:

```
Host azzurra
    HostName login-hpc.univ-cotedazur.fr
    User tjiang
    IdentityFile ~/.ssh/id_tjiangazr
    ProxyCommand nc -X 5 -x localhost:1080 %h %p
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

The two `ServerAlive*` lines are an addition beyond the original rule — keep long ssh-driven sbatch sessions alive across NAT idle timeouts. (Also added to `rules/hpc_azzurra.md` in this session.)

### 1.5 Tunnel + verification

Unattended startup (uses the pass entry, NOPASSWD sudoers rule):

```bash
nohup bash -c "pass show univ-cotedazur/vpn | sudo -n openconnect \
    --passwd-on-stdin \
    --csd-wrapper=/usr/libexec/openconnect/csd-post.sh \
    --user='tjiang@hpc' \
    --script-tun \
    --script 'ocproxy -D 1080' \
    open.unice.fr" >> ~/.cache/magnolia/hpc-tunnel.log 2>&1 &
disown
```

Observed: SOCKS :1080 bound within 2 seconds. `ssh azzurra 'echo ok; whoami; date'` returned cleanly.

This procedure is now wrapped in `opencode_cc_mem/softwares/bin/hpc_tunnel.sh` (see M4 in the plan).

## 2. xtb install on Azzurra

**Deviation from spec:** The spec proposed a native CMake build against Intel/MKL. Mid-execution we switched to the upstream static binary release. Reasoning: xtb runs single-core in our use case; the bundled-MKL vs. cluster-MKL performance gap is <10% on small molecules; the static binary saves 5-8 min of build time and removes all MKL-detection failure modes.

```bash
ssh azzurra '
    VER=6.7.1
    cd ~/src && mkdir -p ~/software/xtb
    wget -q https://github.com/grimme-lab/xtb/releases/download/v$VER/xtb-$VER-linux-x86_64.tar.xz
    wget -q https://github.com/grimme-lab/xtb/releases/download/v$VER/xtb-$VER-linux-x86_64.tar.xz.sha256
    sha256sum -c xtb-$VER-linux-x86_64.tar.xz.sha256
    tar xJf xtb-$VER-linux-x86_64.tar.xz -C ~/software/xtb
    mv ~/software/xtb/xtb-dist ~/software/xtb/$VER
'
```

The tarball extracts to a `xtb-dist/` directory; renamed to `~/software/xtb/6.7.1/` so the install layout is `~/software/xtb/<version>/{bin,lib,share}/`.

Verified the binary runs directly (no module load): `~/software/xtb/6.7.1/bin/xtb --version` prints the standard banner.

## 3. HADDOCK3 + CNS install on Azzurra

**Multiple deviations from spec; recorded in order discovered.**

### 3.1 conda env shell

System-provided `module load miniconda/25.1.1` was used to bootstrap conda (the spec originally proposed installing our own miniforge3 — system module is simpler and admins maintain it).

```bash
ssh azzurra '
    module purge && module load miniconda/25.1.1
    conda create -y -p /workspace/tjiang/envs/magnolia python=3.11
'
```

Path-based env (`-p`, not `-n`) so it lives under `/workspace` (BeeGFS, parallel filesystem). Pinning Python 3.11 to avoid letting HADDOCK3 pick something newer than its deps support.

### 3.2 HADDOCK3 install channel — **deviation: PyPI not bioconda**

The spec considered three channels: PyPI, bioconda, bonvinlab. Probed all three:

- **PyPI**: `pip index versions haddock3` → `2026.5.0` (latest), plus historical versions back to `2025.5.0`. ✅
- **bioconda**: `conda search -c bioconda haddock3` → `No match found`. ❌
- **bonvinlab**: `conda search -c bonvinlab haddock3` → `HTTP 404 NOT FOUND for channel bonvinlab`. ❌

BonvinLab has consolidated on PyPI. Installed via:

```bash
ssh azzurra '
    module purge && module load miniconda/25.1.1
    conda activate /workspace/tjiang/envs/magnolia
    pip install haddock3
'
```

Env size after install: 615 MB. Wall time: ~30s (well-cached wheels).

### 3.3 CNS engine — **deviation: copied from local conda env, not downloaded**

`pip install haddock3` does **not** bundle the CNS executable (license-distribution restriction). HADDOCK3's `haddock.libs.libutil.get_cns_executable()` expects either `<haddock>/cns/bin/{arch}.bin` to exist, or the `CNS_EXEC` env var to point at one.

The local conda env at `~/anaconda3/envs/haddock3-py311/` (installed earlier via the legacy bioconda recipe) **does** ship the CNS binary at `lib/python3.11/site-packages/haddock/cns/bin/x86_64-linux.bin` — 6.4 MB statically-linked ELF for GNU/Linux 3.2.0+, "Special UU release with Rg, paramagnetic" (the BonvinLab academic build).

Copied via the SOCKS proxy:

```bash
ssh azzurra 'mkdir -p /workspace/tjiang/envs/magnolia/lib/python3.11/site-packages/haddock/cns/bin/'
scp -p ~/anaconda3/envs/haddock3-py311/lib/python3.11/site-packages/haddock/cns/bin/x86_64-linux.bin \
    azzurra:/workspace/tjiang/envs/magnolia/lib/python3.11/site-packages/haddock/cns/bin/x86_64-linux.bin
ssh azzurra '
    chmod +x /workspace/tjiang/envs/magnolia/lib/python3.11/site-packages/haddock/cns/bin/x86_64-linux.bin
    sha256sum /workspace/tjiang/envs/magnolia/lib/python3.11/site-packages/haddock/cns/bin/x86_64-linux.bin
'
# SHA256: ce6b0c6b9d38e09991fb15431402e92cb38c723544b2dcca7a8dc28b66643927
```

Licensing: this binary was obtained under the user's academic license; copying to the same user's account on Azzurra is within the same licensing scope (no public redistribution). If a future user joins, they need to obtain their own copy from BonvinLab.

After copy: `haddock.libs.libutil.get_cns_executable()` on Azzurra resolves to the placed path and `exists: True`. CNS banner prints when invoked directly.

## 4. Modulefile setup

```bash
ssh azzurra '
    # Make ~/modulefiles discoverable in login shells
    if ! grep -q "module use.*HOME/modulefiles" ~/.bashrc; then
        printf "\n# Magnolia: discover user-local modulefiles\nmodule use \$HOME/modulefiles\n" >> ~/.bashrc
    fi
'
```

### 4.1 `~/modulefiles/xtb/local.lua`

```lua
help([[
xtb 6.7.1 — official static linux-x86_64 binary release, MKL bundled.
Source: https://github.com/grimme-lab/xtb/releases/tag/v6.7.1
Install: /home/tjiang/software/xtb/6.7.1
SHA256 verified at install time.
No Intel/MKL module needed — the static binary bundles its runtime BLAS.
]])
local version = "6.7.1"
local root = pathJoin(os.getenv("HOME"), "software/xtb", version)
prepend_path("PATH",    pathJoin(root, "bin"))
prepend_path("MANPATH", pathJoin(root, "share/man"))
setenv("XTBPATH",       pathJoin(root, "share/xtb"))
setenv("OMP_NUM_THREADS", os.getenv("OMP_NUM_THREADS") or "1")
conflict("xtb")
```

### 4.2 `~/modulefiles/haddock3/local.lua`

```lua
help([[
HADDOCK3 2026.5.0 via conda env on /workspace/tjiang/envs/magnolia.
Backed by the system `miniconda/25.1.1` module.
CNS engine (HADDOCK-patched, CNSsolve 1.3 UU release) is bundled inside the
env at lib/python3.11/site-packages/haddock/cns/bin/x86_64-linux.bin.
]])
load("miniconda/25.1.1")
local env = "/workspace/tjiang/envs/magnolia"
prepend_path("PATH",        pathJoin(env, "bin"))
setenv("CONDA_PREFIX",      env)
setenv("CONDA_DEFAULT_ENV", "magnolia")
conflict("haddock3")
```

The modulefile manipulates env-vars directly rather than `conda activate`'ing because Lmod modulefiles execute in a subshell where conda's shell hooks aren't sourced. The minimal env-var dance is enough for HADDOCK3 to find its Python interpreter and CNS binary.

## 5. Smoke tests

**Account/QOS gotcha discovered:** The default account is `users` with QOS `suspended` — submissions are rejected (`QOSGrpCpuLimit`). The working account is `spectrometry` with QOS `qos_spectrometry`. All sbatch scripts must include:

```
#SBATCH --account=spectrometry
#SBATCH --qos=qos_spectrometry
```

Without these, submission fails. Discovered via `sacctmgr show association where user=tjiang format=Account,QOS`.

### 5.1 xtb water-optimization

Run dir: `/workspace/tjiang/magnolia/smoke/runs/2026-05-28_xtb_h2o/`
Job ID: **11331410**
Wall time: 2 seconds (00:00:02)
Result: **COMPLETED** (exit 0:0)

Inputs: 3-atom water `h2o.xyz`. Verified output:
- `xtbopt.xyz` written with optimized geometry (energy: -5.0705 Hartree, gnorm: 9.15e-4)
- `.xtboptok` marker file present (convergence flag)
- xtb 6.7.1 banner visible in stdout

### 5.2 HADDOCK3 protein-protein

**Status: PASSED** (job 11331448, gpu partition).

**Path to success — two key discoveries:**

1. **`spectrometry` has gpu-partition access** (initially missed in the long AllowAccounts list). The gpu partition had **136 idle CPUs** even when `cpucourt` was fully allocated. Submitting CPU-only HADDOCK3 work to the gpu partition (without claiming a GPU via `--gres`) is a legitimate way to bypass cpucourt fragmentation.

2. **Use `docking-exit-test.cfg`** (not `docking-protein-protein-test.cfg`) — it has an `[exit]` module immediately after `[rigidbody]`, so HADDOCK3 stops after the cheap modules and skips slow `flexref` / `emref` / clustering. Drops wall time from ~10-15 min to <1 min.

**Initial attempts (deferred):**
- Job 11331412 (16 cores, cpucourt): `PENDING (Priority)` — cluster fully allocated
- Job 11331414 (4 cores, cpucourt): `PENDING (Priority)` — same; cancelled later
- Job 11331446 (4 cores, gpu, sampling=2): **FAILED in 15s** — `Sampling is smaller than the number of model combinations #model_combinations=10, sampling=2`. The hpr ensemble PDB has 5 conformations × 2 receptor combos = 10 combinations; HADDOCK3 requires `sampling >= combinations`. Useful failure: confirms topoaa runs end-to-end (11 CNS topology jobs completed in 2s before rigidbody rejected the config).

**Successful run:** Job **11331448** — `docking-exit-test.cfg`, sampling=10, ncores=4, gpu partition.

| Phase | Wall |
|---|---|
| Queue wait | <10 s |
| `[topoaa]` (11 CNS topology jobs, 4-way parallel) | 2 s |
| `[rigidbody]` (10 docking poses, CNS) | ~32 s |
| `haddock3-traceback` + cleanup | ~4 s |
| **Total cluster wall** | **42 s** |
| **HADDOCK3 internal wall** | **38 s** |

Final state: `COMPLETED` ExitCode `0:0` on `gpu06` (H100 node, used for spare CPUs). Output: `run1-exit-test/{00_topoaa,01_rigidbody,traceback,analysis,log}/`.

**Smoke run dir for inspection:** `/workspace/tjiang/magnolia/smoke/runs/2026-05-28_haddock3_pp_fast/`.

### 5.3 hpc_tunnel.sh idempotency + M4.5 integration test

After extending the NOPASSWD rule (§1.3) to include `pkill -f openconnect*`, both M4.5 and M5.3 ran inline:

| Phase | Result |
|---|---|
| Cold-start (script bootstraps tunnel from down state) | ✅ exit 0, wall 1.36s; **zombie-wrapper detection from M4 fix triggered** (a stale bash wrapper was found but :1080 wasn't bound, so the script correctly fell through to a clean restart instead of false-positiving) |
| Warm-path idempotency (re-invoke while tunnel up) | ✅ exit 0, wall 0.15s, logs `tunnel already up (openconnect process + :1080 bound)` |
| Real ssh through brought-up tunnel | ✅ `tjiang@login-hpc.cluster.local`, kernel 5.14.0, date verified |

The script behaves correctly under the new minimum-friction NOPASSWD policy. Commit `f383fb3` (`fix(hpc): test openconnect-specific sudo permission, not sudo -n true`) was a real bug fix that surfaced during this test — the original check `sudo -n true` was too broad and produced false-negative exit 3 when the NOPASSWD scope was tight.

## 6. Cluster facts discovered (synced into rules/hpc_azzurra.md)

These extended what was previously documented:

- `/workspace` is **BeeGFS** (428T parallel, 83% used cluster-wide).
- **GPU partitions enumerated:** `gpu`, `gpu-icn`, `gpu-icn-prio`, `res-gpu` (six nodes: `gpu[01-06]`, walltimes 1.5–21d).
- **Additional CPU partitions:** `cpucourt-k`, `cpulong-k`, `smp`, `smp-rsc`, `smp-instar`, `amd-k`, `amdcourt-k`, `visu`, `benchmark`, `reservation`.
- **System-provided modules of interest:**
  - `miniconda/25.1.1` (used as our HADDOCK3 env base — no need to install our own miniforge)
  - `intel/2025.2.0` (and 2020–2025.1.1 versions), `gcc/12.2.0`
  - `cmake/4.1.2` (and `cmake/intel/4.1.1`)
  - `python/3.13.2` (default), `python/3.8.1`
  - `openmpi/4.1.7` (default), `openmpi/5.0.8-cuda12.8`
  - `gromacs/2024.1` and `gromacs/2025.1` (when gromacs joins the Magnolia tool stack, no install — just `module load gromacs/2025.1`).
- **Accounts/QOS:**
  - User `tjiang` has two associations: `users` (default, QOS `suspended` — submissions blocked) and `spectrometry` (QOS `qos_spectrometry` — working). All sbatch scripts must specify `--account=spectrometry --qos=qos_spectrometry`.

## 7. Files & paths summary

### Local (WSL)
- `~/.gnupg/` — GPG keyring with key `8A5B495E2406ECC0` + subkey `2E6E0CB768819DF5`
- `~/.password-store/univ-cotedazur/vpn.gpg` — UniCA VPN password
- `/etc/sudoers.d/openconnect` — NOPASSWD rule
- `~/.cache/magnolia/hpc-tunnel.log` — tunnel startup log
- `~/.ssh/config` — `Host azzurra` block with ServerAlive lines

### Azzurra
- `~/software/xtb/6.7.1/` — xtb install (static binary)
- `~/modulefiles/{xtb,haddock3}/local.lua` — user-local modulefiles
- `~/.bashrc` — appended `module use $HOME/modulefiles`
- `~/src/haddock3-source/` — cloned for examples
- `/workspace/tjiang/envs/magnolia/` — conda env with HADDOCK3 + CNS
- `/workspace/tjiang/magnolia/smoke/runs/2026-05-28_xtb_h2o/` — xtb smoke run
- `/workspace/tjiang/magnolia/smoke/runs/2026-05-28_haddock3_pp/` — HADDOCK3 smoke run

### Repo
- `opencode_cc_mem/softwares/bin/hpc_tunnel.sh` — idempotent tunnel manager (committed in `54f3564` on `dev`)
- `opencode_cc_mem/softwares/bin/tests/hpc_tunnel_test.sh` — PATH-stubbed unit tests (6/6 passing)
- `.gitignore` — 3 lines added to allowlist `hpc_tunnel.sh` and the `tests/` dir (the broad `softwares/bin/*` ignore was excluding everything except `magnolia-*`)

## 8. Troubleshooting log

| Symptom | Root cause | Fix |
|---|---|---|
| `pass insert` fails with "Unusable public key" | `gpg --quick-generate-key … default` creates only `[SC]` capabilities, no encryption subkey | Add `[E]` subkey via `gpg --quick-add-key <FPR> rsa4096 encr never` |
| `sbatch` rejects with `QOSGrpCpuLimit` | Default account is `users` with QOS `suspended` | Add `--account=spectrometry --qos=qos_spectrometry` to all SBATCH scripts |
| `haddock3 --version` works but workflow fails with "CNS executable not found" | pip-installed wheel doesn't bundle CNS | Copy CNS from a local bioconda install (or from another working academic install); place at `<haddock>/cns/bin/x86_64-linux.bin` |
| `git add hpc_tunnel.sh` is silently a no-op | `.gitignore` has a broad `opencode_cc_mem/softwares/bin/*` ignore with only `!magnolia-*` allowlisted | Add a specific `!opencode_cc_mem/softwares/bin/hpc_tunnel.sh` negation |
| `pgrep -f openconnect` returns false-positive on the wrapper bash | The `nohup bash -c "...openconnect..."` parent shell matches the `-f` pattern | Require BOTH pgrep match AND port LISTEN before declaring "tunnel up" (fixed in `hpc_tunnel.sh` commit `54f3564`) |
| `hpc_tunnel.sh` exits 3 ("NOPASSWD rule missing") even when openconnect runs fine via sudo | Script tested `sudo -n true` (requires NOPASSWD for `/usr/bin/true`) instead of `sudo -n -l /usr/sbin/openconnect` (tests the actual permission) | Fixed in commit `f383fb3` |

## 9. Replicating this on a fresh account

If you're a future-you or a collaborator setting up from scratch:

1. Get UniCA account + register SSH public key with HPC admins (see `rules/hpc_azzurra.md §2`).
2. Confirm your QOS is **not** `suspended` by running `sacctmgr show association where user=$USER format=Account,QOS` — if it is, ask the admins which account/QOS you should use.
3. Follow §1.1-1.3 of this document to set up `pass` + GPG + NOPASSWD sudoers locally.
4. Add the `Host azzurra` block to `~/.ssh/config` (see §1.4).
5. Run `hpc_tunnel.sh` to bring the VPN up. First run does a cold start (5-30s); subsequent runs are <1s.
6. Follow §2 (xtb static binary), §3 (HADDOCK3 + CNS), §4 (modulefiles) on Azzurra.
7. Smoke-test with §5.1 and §5.2 examples. Both should complete in <15 min.

If anything diverges from this document, prefer **`rules/hpc_azzurra.md`** as the authoritative reference.

## 10. MCP tools for Azzurra (Sub-project B)

Sub-project B (spec: `docs/superpowers/specs/2026-05-29-hpc-azzurra-remote-submission-design.md`, plan: `docs/superpowers/plans/2026-05-29-hpc-azzurra-remote-submission.md`) extends Magnolia's compchem-tools MCP server so the agent can drive Azzurra without the user typing `ssh azzurra '…'` by hand. Four MCP tools are exposed:

| Tool | What it does |
|---|---|
| `submit_job(scheduler="ssh-slurm", command, working_dir, project_dir, cluster="azzurra", tool="haddock3", ...)` | Brings the tunnel up if down, rsyncs the run dir to `/workspace/<user>/magnolia/<project>/runs/<run_id>/`, ssh's `sbatch`, returns the Slurm job_id. Writes `runs/<id>.yaml` with `lifecycle: submitted` and full `remote:` block. |
| `check_job(job_id, scheduler="ssh-slurm", cluster="azzurra", project_dir=...)` | ssh sacct → state + sacct resource fields (max_rss, elapsed, ave_cpu, node). Falls back to squeue for in-queue jobs. Updates the YAML's `lifecycle` and `remote.slurm.*` fields. Returns `terminal: bool`. |
| `cancel_job(job_id, scheduler="ssh-slurm", cluster="azzurra", project_dir=...)` | ssh scancel; sets the YAML to `lifecycle: cancelled`. |
| `fetch_job_results(job_id, project_dir=...)` | Looks up the run by job_id, rsyncs the remote run dir back to its recorded local path, updates YAML to `lifecycle: fetched` and stamps `remote.fetched_at`. |

`runs/INDEX.yaml` is auto-maintained as flow-style YAML (one record per line) with the new `cluster`, `job_id`, `slurm_state`, `elapsed`, `node` columns. `grep azzurra runs/INDEX.yaml` returns full rows; `grep TIMEOUT runs/INDEX.yaml` lists timeouts with full identifying context per line.

**What's NOT in B** (deferred to Sub-project C):
- Auto-polling without a live session
- Auto-fetch on terminal state
- Auto `post_run_assess` after fetch

For manual ad-hoc work, the shell aliases from §1.5 (`azzurra-status`, `azzurra`) remain useful — they're independent of the MCP tools.

### Restart discipline

Because B touches both MCP server packages (compchem-memory and compchem-tools), opencode must be restarted after merging to master to pick up the changes:
- After M1 lands on master: restart for compchem-memory's new `record_run` kwargs + `update_run` + flow-style INDEX
- After M7 lands on master: restart for compchem-tools' ssh-slurm dispatch + `fetch_job_results` tool

A single restart after the full Sub-project B merge covers both.

### Test coverage

Sub-project B ships with:
- **47 unit tests** in `compchem-tools/tests/test_ssh_slurm.py` covering all four entry points + helpers + 4 error paths + 3 dispatch tests + 1 MCP wrapper test. Uses a `fake_subprocess` fixture (in `tests/conftest.py`) to avoid touching the real network.
- **6 unit tests** in `compchem-memory/tests/test_project_extensions.py` for the ProjectManager extensions, including a backward-compat test that verifies legacy callers continue to work.
- **1 gated integration test** in `compchem-tools/tests/test_ssh_slurm_integration.py` that runs against the live cluster when `MAGNOLIA_INTEGRATION_AZZURRA=1` is set. Submits a tiny `echo + sleep + cat` job to the `gpu` partition, polls to completion, fetches results, verifies on-disk content.

Run the gated test with:
```
cd opencode_cc_mem/mcp-servers/compchem-tools && \
    MAGNOLIA_INTEGRATION_AZZURRA=1 \
    PYTHONPATH=src:../compchem-memory/src \
    pytest tests/test_ssh_slurm_integration.py -v -s
```

## 11. Async job lifecycle (Sub-project C)

After Sub-project C, the `compchem-tools` MCP server runs a background
daemon thread that polls all ssh-slurm jobs in this project on a wall-clock
interval. No more manual `check_job → fetch_job_results → post_run_assess`
chains — submit a job and walk away.

### Interval & env vars
- `MAGNOLIA_POLL_INTERVAL_MIN` (default `5`) — how often to sweep.
- Set to `0`/negative/non-int → falls back to default.

### Lifecycle (per run YAML's `lifecycle` field)
- `submitting` — write-ahead breadcrumb; sbatch in flight.
- `submitted` — sbatch returned a job_id, queued.
- `running` — `sacct` reports R/PD/CF/S/CG.
- `completed` — terminal success (sacct `COMPLETED`).
- `failed` — terminal failure (science, infra, or unknown state).
- `cancelled` — terminal via scancel.
- `fetched` — results rsynced back; for success, `assess_and_record` ran.

### Terminal-state policy
| sacct state | Action |
|---|---|
| `COMPLETED` | fetch + assess (sets scientific `status`) |
| `FAILED` / `TIMEOUT` / `OUT_OF_MEMORY` / `REVOKED` | fetch + capture log tails + staging memory entry |
| `NODE_FAIL` / `BOOT_FAIL` / `PREEMPTED` / `DEADLINE` | flag `remote.retry_recommended=true`; no fetch |
| `CANCELLED` (by you) | record state; no fetch |
| anything else | conservative — fetch + capture |

### Self-describing remote dirs (L4)
Every remote run dir contains:
- `.magnolia/manifest.json` — run_id, tool, project, account/qos/partition, command, submitted_at.
- `.magnolia/jobid` — stamped by the job at runtime via `echo "$SLURM_JOB_ID"`.

So even if the local YAML is lost, you can `rsync` the remote dir and
reconstruct the run.

### Operator notes
- The poller runs **only while opencode is open** (it lives inside the
  `compchem-tools` MCP subprocess). Jobs that finish during a closure are
  fetched on the next opencode start (a startup catch-up sweep runs
  immediately, before the interval loop). If the cluster scratch is purged
  before reopen, results are lost — see `todo.md` L5 (deferred reconcile).
- Restart opencode after merging C to master so the MCP subprocess picks
  up the new tool + timer.
- Force an immediate sweep with the MCP tool `poll_jobs` (or set a
  shorter interval temporarily via `MAGNOLIA_POLL_INTERVAL_MIN`).
- If `submit_job` fails at the rsync or sbatch stage, a `lifecycle=submitting`
  breadcrumb YAML remains in `<project>/.magnolia/runs/`. The poller correctly
  skips these (no `job_id`). To find them:
  `grep -l "lifecycle: submitting" <project>/.magnolia/runs/*.yaml`.
  Delete what looks orphaned.

### Test coverage
- Unit (no network): `compchem-memory` — `tests/test_atomic_io.py`,
  `tests/test_atomic_record_run.py`, `tests/test_index_robustness.py`,
  `tests/test_orchestrator.py`, `tests/test_post_run_assess_parity.py`.
  `compchem-tools` — `tests/test_poller.py`, `tests/test_poll_jobs_tool.py`,
  `tests/test_submit_writeahead.py`, `tests/test_sbatch_template.py`.
- Gated integration: `MAGNOLIA_INTEGRATION_AZZURRA=1`
  → `tests/test_ssh_slurm_integration.py::test_poll_drives_short_job_to_fetched`
  + `test_poll_captures_failed_job`.
