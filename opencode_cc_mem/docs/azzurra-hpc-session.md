# Azzurra transport: optional hpc-session backend

Date: 2026-11-13
Branch: `exp/idea-verified-run-promotion`

## Why this exists

The always-on agent needs a **stable, unattended** Azzurra connection. The
previous model — a human opens an SSH ControlMaster once every ~10 h and tools
piggyback on it — fails with `master_down` as soon as the master expires. That
is fine for interactive work, but it stalls a daemon overnight.

`hpc-session` owns the connection and its TOTP handling, so the daemon no
longer depends on a human reopening a window. compchem-tools can now use it as
an **optional** transport backend.

## Design

`clusters.yaml` gains a per-cluster `transport` key:

```yaml
clusters:
  azzurra:
    transport: hpc-session   # default: ssh
```

Default is `ssh`; nothing changes unless you opt in.

When `transport: hpc-session` is set:

- `_ssh` → `hpc-session run -- <command>`
- `_rsync_push` / `_rsync_pull` → `hpc-session local rsync ...`
- `_ensure_master` → `hpc-session status` (instead of `ssh -O check`)

The VPN layer is unchanged: `hpc_tunnel.sh` still brings up the SOCKS5 split
tunnel on `localhost:1080`. Non-2FA clusters keep the existing
`requires_control_master: false` path.

This is not a hard dependency. `hpc-session` is only invoked for clusters whose
profile selects it. Install and configure it before enabling the backend.

## TOTP policy for unattended use

`hpc-session` can answer TOTP via `SSH_ASKPASS`. For the daemon, use a
`command` backend with the seed stored in `pass` (GPG-encrypted), not in a
plaintext file beside the SSH key:

```bash
export HS_TOTP_BACKEND=command
export HS_TOTP_COMMAND='pass show univ-cotedazur/totp | oathtool --totp -'
```

If you have a hardware token or an external generator, use that instead. If you
do not want to store a seed at all, the only stable unattended alternatives are
an admin-granted auth exemption or running the agent on the cluster itself.

## QOS

`default_qos: ""` is intentional and stays. On Azzurra, Slurm auto-assigns QOS
from the account association; passing an explicit `--qos=qos_spectrometry`
triggers `QOSGrpCpuLimit`.

## Setup for the daemon profile

1. Install `hpc-session` and verify `hpc-session doctor`.
2. Set `HS_CONTROL_DIR` (default `~/.ssh/hpc-session`) and the TOTP backend
   above.
3. Add to `~/.config/magnolia/clusters.yaml`:

   ```yaml
   clusters:
     azzurra:
       transport: hpc-session
   ```

4. `hpc-session open` once per work window. After that, `submit_job`,
   `check_job`, and `fetch_job_results` use the hpc-session master and should
   not ask for a second code.

## Tests

`compchem-tools/tests/test_ssh_slurm_hpc_session.py` pins the backend and
asserts the CLI delegation. Existing `test_ssh_slurm.py` and
`test_ssh_slurm_generic.py` confirm the default ssh path is unchanged.
