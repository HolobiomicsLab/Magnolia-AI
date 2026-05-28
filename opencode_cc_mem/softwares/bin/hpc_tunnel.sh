#!/usr/bin/env bash
# hpc_tunnel.sh — idempotent VPN tunnel manager for the Azzurra HPC cluster.
#
# Contract: see opencode_cc_mem/rules/hpc_azzurra.md (Tunnel Health-Check Helper).
# Exit codes:
#   0  tunnel is up (already, or just started)
#   2  `pass` entry missing
#   3  sudo without password failed (NOPASSWD sudoers rule missing)
#   4  tunnel started but did not bind :1080 within TIMEOUT_S seconds
#   5  neither UNICA_USER nor USER is set (cannot determine UniCA login)
#
# Usage: hpc_tunnel.sh           # uses $USER as the UniCA login
#        UNICA_USER=foo hpc_tunnel.sh
#        TIMEOUT_S=5 hpc_tunnel.sh   # override port-bind poll timeout (default 30s)
set -uo pipefail

UNICA_USER="${UNICA_USER:-${USER:-}}"
LOG_FILE="$HOME/.cache/magnolia/hpc-tunnel.log"
CSD_WRAPPER="/usr/libexec/openconnect/csd-post.sh"
PASS_ENTRY="***"
PORT=1080
TIMEOUT_S="${TIMEOUT_S:-30}"

log() { echo "[hpc_tunnel] $*" >&2; }

if [ -z "$UNICA_USER" ]; then
    log "ERROR: neither UNICA_USER nor USER is set. Pass UNICA_USER=<login> hpc_tunnel.sh."
    exit 5
fi

# 1. Already running AND port bound?
if pgrep -f 'openconnect.****' >/dev/null 2>&1; then
    if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
        log "tunnel already up (openconnect process + :$PORT bound)"
        exit 0
    else
        log "openconnect process found but :$PORT not bound; will attempt restart"
        # Fall through to the normal startup path.
    fi
fi

# 2. Port already listening (not by us)?
if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    log "SOCKS :$PORT already listening (not started by this script)"
    exit 0
fi

# 3a. pass entry available?
if ! pass show "$PASS_ENTRY" >/dev/null 2>&1; then
    log "ERROR: \`pass show $PASS_ENTRY\` failed. Set up with:"
    log "       pass insert $PASS_ENTRY"
    exit 2
fi

# 3b. sudo without password?
if ! sudo -n true 2>/dev/null; then
    log "ERROR: \`sudo -n true\` failed. Add the NOPASSWD sudoers rule:"
    log "       echo '$UNICA_USER ALL=(root) NOPASSWD: /usr/sbin/openconnect' | sudo tee /etc/sudoers.d/openconnect"
    log "       sudo chmod 0440 /etc/sudoers.d/openconnect"
    exit 3
fi

# 3c. Start the tunnel.
mkdir -p "$(dirname "$LOG_FILE")"
log "starting openconnect (logs: $LOG_FILE)"
nohup bash -c '
    pass show "$1" | sudo -n openconnect \
        --passwd-on-stdin \
        --csd-wrapper="$2" \
        --user="$3@realm" \
        --script-tun \
        --script "ocproxy -D $4" \
        ***
' _ "$PASS_ENTRY" "$CSD_WRAPPER" "$UNICA_USER" "$PORT" \
    >>"$LOG_FILE" 2>&1 &
disown

# 4. Poll for the port to come up.
for i in $(seq 1 $TIMEOUT_S); do
    if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
        log "tunnel up (bound :$PORT after ${i}s)"
        exit 0
    fi
    sleep 1
done

log "ERROR: tunnel did not bind :$PORT within ${TIMEOUT_S}s; see $LOG_FILE"
exit 4
