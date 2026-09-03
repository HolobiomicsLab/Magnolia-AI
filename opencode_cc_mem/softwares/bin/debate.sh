#!/usr/bin/env bash
# debate.sh — orchestrate blind adversarial debate rounds for Magnolia.
#
# A "debate" is a Magnolia run directory:
#   $MAGNOLIA_ROOT/$MAGNOLIA_PROJECT_DIR/runs/YYYY-MM-DD_debate-<name>/
# containing:
#   ctx/       the adversary's project dir (context files copied here; the
#              adversary can only read inside its project dir)
#   prompts/   round prompt files (r1.txt, r2.txt, ...)
#   out/       per-round opencode JSON event streams (r1.json, ...)
#   err/       per-round stderr + final EXIT=<code> marker
#   meta.env   bookkeeping: ROUNDS, MODEL_rN, PID_rN, SID_rN
#   verdict.md (written by the agent at finalize time, not by this script)
#
# Smoke-test workspaces may live in /tmp/debate-<name> (init --tmp); `clean`
# only removes those — run directories under runs/ are records, never deleted.
#
# Rounds run headless: `opencode run --format json -m <model>` executed INSIDE
# ctx/ with OPENCODE_DISABLE_PROJECT_CONFIG=1, so the adversary loads none of
# Magnolia's plugins/rules/memory (blindness). Round 2+ resumes the same
# opencode session (-s) so the adversary keeps its context.
#
# All launch commands return immediately; poll with `status`. Never blocks.
set -u

die() { echo "debate.sh: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
debate.sh — blind adversary orchestration for Magnolia

  init   <name> [--tmp] <context-file>...   create workspace, copy context files into ctx/
                                            (default: <project>/runs/YYYY-MM-DD_debate-<name>/;
                                             --tmp uses /tmp/debate-<name> for smoke tests)
  round1 <name> <model> <prompt-file>       launch round 1 (fresh blind session), background
  resume <name> <model> <prompt-file>       resume the session with the next round prompt
  status <name>                             latest round: running | done | failed (+detail)
  answer <name> [round]                     print final assistant text of a round (default: last)
  sessions <name>                           list session ids captured per round
  finalize <name>                           verify all rounds done; print run-record JSON +
                                            aftermath checklist (verdict.md, memory_record_run)
  clean  <name>                             remove a --tmp smoke workspace (refuses runs/ dirs)

Model is provider/model as in `opencode models` (e.g. kimi-for-coding/k3).
Prompt files: plain text; reference context files by bare filename (they are
copied into the adversary's cwd). Poll status every ~60-90 s; a round
typically takes 3-8 minutes.

Workspace resolution: commands locate the workspace by name — /tmp first,
then <project>/runs/*_debate-<name> (must match exactly one).
EOF
  exit 0
}

cmd="${1:-}"; shift || true
case "$cmd" in
  init|round1|resume|status|answer|sessions|finalize|clean) ;;
  -h|--help|help|"") usage ;;
  *) die "unknown command: $cmd (see --help)" ;;
esac

NAME="${1:-}"; [ -n "$NAME" ] || die "missing <name>"
case "$NAME" in */*|*..*|*" "*) die "bad name: $NAME" ;; esac

proj_runs_base() {
  local proj="${MAGNOLIA_PROJECT_DIR:-}" root="${MAGNOLIA_ROOT:-}"
  [ -n "$proj" ] && [ -n "$root" ] || \
    die "MAGNOLIA_ROOT / MAGNOLIA_PROJECT_DIR not set; run via compchem-tools_run_shell inside a Magnolia session"
  if [ "${proj:0:1}" = "/" ]; then
    echo "$proj/runs"; return 0
  fi
  # relative project dir: the canonical project home is $root/opencode_cc_mem/$proj
  # (gitignored research data). A bare $root/$proj can exist as a stray memory
  # artifact (created when a tool resolved the path against the repo root) —
  # never write run output there.
  local cand
  for cand in "$root/opencode_cc_mem/$proj" "$PWD/$proj" "$root/$proj"; do
    if [ -d "$cand" ]; then
      [ "$cand" = "$root/$proj" ] && echo "debate.sh: WARNING: falling back to $cand (no opencode_cc_mem/$proj found)" >&2
      echo "$cand/runs"; return 0
    fi
  done
  die "cannot resolve MAGNOLIA_PROJECT_DIR=$proj (tried $root/opencode_cc_mem/$proj, $PWD/$proj, $root/$proj)"
}

# Locate an existing workspace by name; sets ROOT.
resolve_root() {
  if [ -d "/tmp/debate-$NAME" ]; then ROOT="/tmp/debate-$NAME"; return 0; fi
  local base; base="$(proj_runs_base)"
  shopt -s nullglob
  local matches=( "$base/"*_debate-"$NAME" )
  shopt -u nullglob
  [ "${#matches[@]}" -eq 1 ] || \
    die "expected exactly 1 workspace matching *_debate-$NAME under $base, found ${#matches[@]}"
  ROOT="${matches[0]}"
}

# Append SID_rN to meta.env if the events file already streamed it.
capture_sid() { # $1 = round number
  local r="$1" sid
  grep -q "^SID_r$r=" "$ROOT/meta.env" 2>/dev/null && return 0
  [ -s "$ROOT/out/r$r.json" ] || return 0
  sid=$(python3 - "$ROOT/out/r$r.json" <<'PY' 2>/dev/null || true
import json, sys
for line in open(sys.argv[1]):
    try:
        e = json.loads(line)
    except Exception:
        continue
    sid = e.get("sessionID")
    if sid:
        print(sid); break
PY
)
  [ -n "${sid:-}" ] && echo "SID_r$r=$sid" >> "$ROOT/meta.env"
}

case "$cmd" in
init)
  shift
  TMP=0
  [ "${1:-}" = "--tmp" ] && { TMP=1; shift; }
  if [ "$TMP" -eq 1 ]; then
    ROOT="/tmp/debate-$NAME"
  else
    ROOT="$(proj_runs_base)/$(date +%F)_debate-$NAME"
  fi
  [ -e "$ROOT" ] && die "$ROOT already exists (clean first, or pick a new name)"
  mkdir -p "$ROOT/ctx" "$ROOT/prompts" "$ROOT/out" "$ROOT/err" || die "mkdir failed"
  echo "ROUNDS=0" > "$ROOT/meta.env"
  for f in "$@"; do
    [ -f "$f" ] || die "context file not found: $f"
    cp "$f" "$ROOT/ctx/" || die "copy failed: $f"
  done
  echo "initialized $ROOT ($(ls "$ROOT/ctx" | wc -l) context file(s))"
  ;;

round1|resume)
  MODEL="${2:-}"; PF="${3:-}"
  [ -n "$MODEL" ] && [ -n "$PF" ] || die "usage: debate.sh $cmd <name> <model> <prompt-file>"
  resolve_root
  [ -f "$PF" ] || die "prompt file not found: $PF"
  # shellcheck disable=SC1091
  source "$ROOT/meta.env"
  if [ "$cmd" = round1 ]; then
    [ "${ROUNDS:-0}" -eq 0 ] || die "round 1 already ran; use resume for round 2+"
  else
    [ "${ROUNDS:-0}" -ge 1 ] || die "no round 1 yet; run round1 first"
  fi
  R=$(( ROUNDS + 1 ))
  RESUME_ARGS=()
  if [ "$cmd" = resume ]; then
    P=$(( R - 1 ))
    capture_sid "$P"
    # shellcheck disable=SC1090
    source "$ROOT/meta.env"
    PSID_VAR="SID_r$P"
    PSID="${!PSID_VAR:-}"
    [ -n "$PSID" ] || die "previous round session id not captured yet; retry shortly"
    RESUME_ARGS=(-s "$PSID")
  fi
  cp "$PF" "$ROOT/prompts/r$R.txt"
  LOG="$ROOT/err/r$R.log"
  (
    cd "$ROOT/ctx" || exit 1
    OPENCODE_DISABLE_PROJECT_CONFIG=1 opencode run -m "$MODEL" --format json \
      --title "debate-$NAME-r$R" ${RESUME_ARGS+"${RESUME_ARGS[@]}"} \
      "$(cat "$ROOT/prompts/r$R.txt")" \
      > "$ROOT/out/r$R.json" 2> "$LOG"
    echo "EXIT=$?" >> "$LOG"
  ) >/dev/null 2>&1 &
  PID=$!
  echo "$PID" > "$ROOT/pid_r$R"
  sed -i "s/^ROUNDS=.*/ROUNDS=$R/" "$ROOT/meta.env"
  echo "MODEL_r$R=$MODEL" >> "$ROOT/meta.env"
  echo "PID_r$R=$PID" >> "$ROOT/meta.env"
  echo "launched round $R (model $MODEL, pid $PID); poll: debate.sh status $NAME"
  ;;

status)
  resolve_root
  # shellcheck disable=SC1091
  source "$ROOT/meta.env"
  R="${ROUNDS:-0}"
  [ "$R" -ge 1 ] || { echo "no rounds launched yet"; exit 0; }
  capture_sid "$R"
  PID_VAR="PID_r$R"; PID="${!PID_VAR:-}"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "round $R: RUNNING (pid $PID, out=$(wc -c < "$ROOT/out/r$R.json" 2>/dev/null || echo 0) bytes)"
    tail -n 1 "$ROOT/err/r$R.log" 2>/dev/null | head -c 200; echo
  elif grep -q "^EXIT=0$" "$ROOT/err/r$R.log" 2>/dev/null; then
    echo "round $R: DONE ($(wc -c < "$ROOT/out/r$R.json" 2>/dev/null || echo 0) bytes out)"
  else
    echo "round $R: FAILED"
    tail -n 3 "$ROOT/err/r$R.log" 2>/dev/null | head -c 400; echo
  fi
  ;;

answer)
  R="${2:-}"
  resolve_root
  # shellcheck disable=SC1091
  source "$ROOT/meta.env"
  [ -n "$R" ] || R="${ROUNDS:-0}"
  [ "$R" -ge 1 ] || die "no rounds to extract"
  grep -q "^EXIT=0$" "$ROOT/err/r$R.log" 2>/dev/null || die "round $R not finished cleanly (status $NAME)"
  python3 - "$ROOT/out/r$R.json" <<'PY'
import json, sys
texts = []
for line in open(sys.argv[1]):
    try:
        e = json.loads(line)
    except Exception:
        continue
    p = e.get("part") or {}
    if p.get("type") == "text" and p.get("text"):
        texts.append(p["text"])
sys.stdout.write(texts[-1] if texts else "")
PY
  ;;

sessions)
  resolve_root
  for r in $(seq 1 "$(( $(grep -c '^SID_r' "$ROOT/meta.env" 2>/dev/null || echo 0) ))"); do
    grep "^SID_r$r=" "$ROOT/meta.env" || true
  done
  grep '^MODEL_r' "$ROOT/meta.env" || true
  ;;

finalize)
  resolve_root
  # shellcheck disable=SC1091
  source "$ROOT/meta.env"
  R="${ROUNDS:-0}"
  [ "$R" -ge 1 ] || die "no rounds launched; nothing to finalize"
  for r in $(seq 1 "$R"); do
    grep -q "^EXIT=0$" "$ROOT/err/r$r.log" 2>/dev/null || die "round $r not cleanly done (see err/r$r.log); finalize aborted"
  done
  python3 - "$ROOT" "$R" <<'PY'
import json, os, re, sys
root, R = sys.argv[1], int(sys.argv[2])
meta = open(os.path.join(root, "meta.env")).read()
models = re.findall(r"^MODEL_r\d+=(.+)$", meta, re.M)
sids = re.findall(r"^SID_r\d+=(.+)$", meta, re.M)
nbytes = sum(os.path.getsize(os.path.join(root, "out", f)) for f in os.listdir(os.path.join(root, "out")))
record = {
    "run_id": os.path.basename(root),
    "tool": "debate",
    "status": "success",
    "run_dir": root,
    "metrics": {"rounds": R, "models": models, "opencode_sessions": sids, "transcript_bytes": nbytes},
}
print(json.dumps(record, indent=2))
PY
  cat <<EOF
---
Aftermath checklist (the agent does these, not the script):
1. Write the merged verdict to $ROOT/verdict.md
2. memory_record_run(tool="debate", run_id="$(basename "$ROOT")", status="success", metrics from the JSON above)
3. memory_record_learning(entry_type="note", ...) with the converged outcome + pointer to the run dir
4. Save durable round answers: debate.sh answer $NAME <round> > file (or reference out/rN.json)
EOF
  ;;

clean)
  [ -d "/tmp/debate-$NAME" ] || die "clean only removes --tmp workspaces (/tmp/debate-<name>); runs/ directories are records — do not delete"
  rm -rf "/tmp/debate-$NAME"
  echo "removed /tmp/debate-$NAME"
  ;;
esac
