#!/usr/bin/env bash
# Supervisor for the compchem-tools MCP server running as an HTTP daemon
# (fastmcp streamable-http on 127.0.0.1:8001).
#
# Why a daemon instead of opencode-spawned stdio:
#   opencode aborts tool calls by closing the stdio pipe; the serve-loop
#   watchdog (server.py:_serve_resilient) then sees near-instant disconnects
#   and exits, and opencode never respawns a dropped *local* MCP server —
#   every compchem-tools tool vanishes for the session. As a remote HTTP
#   server the process outlives client aborts, and this supervisor restarts
#   it if it ever does die. opencode.json declares it as
#   {"type": "remote", "url": "http://127.0.0.1:8001/mcp"}.
#
# Usage:
#   compchem-tools-daemon.sh start | stop | status | restart
#
# The `magnolia` launch wrapper calls `start` automatically before exec'ing
# opencode (idempotent, ~instant when already running), so no manual step is
# needed in day-to-day use.
#
# NOTE: MAGNOLIA_PROJECT_DIR comes from the caller's environment (the `magnolia`
# wrapper exports it) and defaults to projects/communication. It only pins the
# background poller's default project — tools accept project_dir per-call.

set -u
# softwares/bin/<script> → three levels up = repo root (…/project_magnolia).
# Two levels up would be opencode_cc_mem itself — the .venv and opencode_cc_mem/
# subdir paths below would then double up (bug fixed 2026-08-19).
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PY="$ROOT/.venv/bin/python3"
PORT="${COMPCHEM_TOOLS_PORT:-8001}"
HOST="127.0.0.1"
PROJECT_DIR="${MAGNOLIA_PROJECT_DIR:-projects/communication}"
LOG_DIR="$ROOT/opencode_cc_mem/logs"
OUT_LOG="$LOG_DIR/compchem-tools-http.log"
PIDFILE="${XDG_RUNTIME_DIR:-/tmp}/compchem-tools-daemon-${PORT}.pid"

is_up() {
  # Proxy-proof liveness: OUR supervisor process must be alive. HTTP port
  # checks are unreliable here — a transparent proxy (Clash TUN) can answer
  # for 127.0.0.1 even when nothing listens locally, producing a false
  # "already running" that prevents the supervisor from ever starting
  # (observed 2026-08-19).
  if [ -f "$PIDFILE" ]; then
    sup="$(cat "$PIDFILE" 2>/dev/null)"
    [ -n "$sup" ] && kill -0 "$sup" 2>/dev/null && return 0
  fi
  return 1
}

port_up() {
  # Readiness only (used in the start() wait loop). Best-effort: a
  # transparent proxy may answer even when the server is down, but the wait
  # loop then just returns early; the supervisor keeps running regardless.
  code=$(curl --noproxy '*' -s -o /dev/null -w "%{http_code}" --max-time 2 "http://$HOST:$PORT/" 2>/dev/null || echo 000)
  [ "$code" != "000" ]
}

status() {
  if is_up; then
    echo "compchem-tools MCP daemon: UP (supervisor pid $(cat "$PIDFILE")) on http://$HOST:$PORT/mcp"
    return 0
  fi
  echo "compchem-tools MCP daemon: DOWN (no supervisor process)"
  return 1
}

start() {
  if is_up; then
    echo "already running (supervisor pid $(cat "$PIDFILE")) on http://$HOST:$PORT/mcp"
    return 0
  fi
  # Clear stale pidfile from a dead supervisor.
  rm -f "$PIDFILE"
  mkdir -p "$LOG_DIR"
  # Supervisor loop: restart the server if it ever exits (crash or wedge).
  # Runs as its own session leader so it survives logout. Env is baked into
  # the child-script text (shell functions don't cross into bash -c children).
  SERVER_ENV="MAGNOLIA_ROOT='$ROOT' MAGNOLIA_RULES_DIR='rules' MAGNOLIA_PROJECT_DIR='$PROJECT_DIR' COMPCHEM_TOOLS_TRANSPORT='http' COMPCHEM_TOOLS_HOST='$HOST' COMPCHEM_TOOLS_PORT='$PORT' PYTHONPATH='${PYTHONPATH:-}' PATH='$ROOT/opencode_cc_mem/softwares/bin:$PATH'"
  setsid bash -c "
    echo \"[supervisor] \$(date -Is) start\" >> '$OUT_LOG'
    while true; do
      env $SERVER_ENV '$PY' -m compchem_tools.server >> '$OUT_LOG' 2>&1
      echo \"[supervisor] \$(date -Is) server exited rc=\$?; restarting in 2s\" >> '$OUT_LOG'
      sleep 2
    done
  " &
  sup_pid=$!
  echo "$sup_pid" > "$PIDFILE"
  # Wait for the HTTP endpoint to come up (server import + bind can take a
  # few seconds). On timeout the supervisor still runs — the warning is
  # informational only.
  for _ in $(seq 1 30); do
    port_up && echo "started: supervisor pid=$sup_pid, http://$HOST:$PORT/mcp" && return 0
    sleep 1
  done
  echo "supervisor pid=$sup_pid running but port not answering after 30s; check $OUT_LOG (a transparent proxy may fake the port check — verify with: ps aux | grep compchem_tools)"
  return 0
}

stop() {
  if [ -f "$PIDFILE" ]; then
    sup="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "${sup:-}" ]; then
      kill "$sup" 2>/dev/null || true
      # The supervisor is a session leader (setsid): killing its process
      # group takes the `env ... compchem_tools.server` child with it.
      # Scoped by pidfile — never pkill globally (a global pkill would kill
      # the live daemon's server on 8001 when stopping this instance).
      kill -- "-$sup" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
  fi
  sleep 1
  if is_up; then echo "still up?"; return 1; fi
  echo "stopped"
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  *) echo "usage: $0 {start|stop|status|restart}"; exit 2 ;;
esac
