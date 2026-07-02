# run_shell timeout → submit_job redirect (routing gate, step 1)

**Date:** 2026-06-25
**Status:** Design approved, pre-implementation
**Scope:** Reactive routing enforcement only. Memory-recall gate is a separate follow-up spec.

## Problem

The compchem agent will not reliably route long-running local work through
`submit_job`, no matter how the prose instruction is worded. This is the
enforcement-gap pattern (see project memory `magnolia-enforcement-gap-direction`):
prose rules do not bind; only mechanical gates do.

The expensive work is **not** identifiable by entrypoint. It is sometimes a
recognizable analysis script (`python analyze.py`, `Rscript`), but just as often
a plain shell loop firing many small jobs (e.g. a `for` loop over 50 `gnina`/`vina`
dockings). These share no matchable program name. The only property they share is
**duration**. Therefore the gate must key on observed runtime, not on command
content — a predictive entrypoint allowlist was explicitly rejected because it
cannot see a docking loop and would also misfire on quick scripts.

## Goal

When a command run via `run_shell` exceeds the foreground time limit, turn the
current dead-end ("timed out") into an actionable redirect that tells the agent
to relaunch the work through `submit_job(scheduler="local")`, which **backgrounds
it and makes it pollable** (PID-based status, log files). Quick commands are
unaffected and keep returning their output inline.

NOTE on "tracked": `scheduler="local"` jobs are backgrounded and pollable, but
they are **outside the ssh-slurm run-record lifecycle** — no `.magnolia/runs/`
record, not scanned by the background poller (`_scan_active_runs` filters to
`scheduler == "ssh-slurm"`), and no automatic completion→assess→memory-log /
failure-capture. `check_run_status` falls back to local-file inspection for them.
Bringing local jobs into that lifecycle is the **write side of step 2** (see
Follow-ups), not part of this spec.

## Non-goals

- Memory-recall / pitfall warnings before costly actions (separate step-2 spec).
- Any *proactive* prediction that a command will be long (reactive only).
- `write`/`edit` soft-injection.
- The opencode plugin layer (`tool.execute.before`). Kept in reserve per the
  Hybrid enforcement-layer decision; not used here. All enforcement in this spec
  is pure Python inside the MCP tool we own.

## Grounding facts (verified 2026-06-25)

- `run_shell` ([tools/shell.py](../../mcp-servers/compchem-tools/src/compchem_tools/tools/shell.py))
  is our own MCP tool (opencode's built-in `bash` is disabled via
  `"tools": {"bash": false}`). It is synchronous, returns a ≤4 KB stdout/stderr
  tail inline, and has a **hard never-raise contract**: any exception escaping
  into the fastmcp layer takes down the whole MCP server. Every path returns a
  structured dict with `error_kind`.
- Current timeout: `_DEFAULT_TIMEOUT = 600` (10 min). On `TimeoutExpired` the tool
  already kills the command and returns
  `{exit_code:-1, stdout, stderr, error_kind:"timeout", error:"command timed out after 600s"}`.
- `submit_job` ([tools/jobs.py](../../mcp-servers/compchem-tools/src/compchem_tools/tools/jobs.py))
  supports `scheduler="local"` via `_submit_local`, which backgrounds the command
  with `subprocess.Popen`, returns a `job_id` (`local_<pid>_<hex>`) immediately,
  writes stdout/stderr to `<job_name>.out/.err` in the working dir, and is polled
  via `_check_local` (PID-based).

## Design

### Change site

One file: `tools/shell.py`. Only the `subprocess.TimeoutExpired` branch changes.
The magnolia-run proxy, the success path, the other error branches, the
never-raise contract, and the agent-facing signature are all untouched. No new
tool, no plugin.

### New pure helper

```python
def _build_local_redirect(cmd: str, cwd: str | None, timeout: int) -> dict[str, Any]:
    """Pure (no I/O). Returns the fields to merge into the timeout result dict
    that redirect the agent to submit_job(scheduler='local'). `timeout` is passed
    in (not read from the module constant) so the message and the test stay in
    lockstep under monkeypatch."""
```

It returns:

- `error` (string) — actionable, replacing the dead-end message:
  > `command exceeded the {timeout}s foreground limit. Long runs — including`
  > `batches of many small jobs, e.g. a docking loop — must run in the background`
  > `via submit_job(scheduler="local"). Do NOT re-run this via run_shell; it will`
  > `time out again. Relaunch using the suggested_action below; set ncores/memory`
  > `appropriate to the workload.`
- `suggested_action` (dict) — ready-to-issue call template:
  ```python
  {"tool": "submit_job",
   "args": {"command": cmd,
            "working_dir": cwd or os.getcwd(),
            "scheduler": "local"}}
  ```
  `ncores`/`memory`/`job_name` are deliberately omitted — the agent sets them per
  workload; we do not guess.

### Timeout branch

The existing `TimeoutExpired` handler keeps `exit_code:-1`, the partial truncated
`stdout`/`stderr`, and `error_kind:"timeout"` (preserving backward compatibility
for anything keying on that), then merges in the keys from `_build_local_redirect`.
Net change: `error` becomes actionable and a `suggested_action` field is added.
Schema is strictly additive.

### Loop-safety & caveats (stated, not coded in v1)

- **Loop risk:** the explicit "do NOT re-run via run_shell" sentence is the v1
  mitigation. `run_shell` stays stateless — it does not remember prior timeouts.
  If loop-pinging is observed in practice, add state in a follow-up.
- **Re-run from scratch:** relaunching re-executes from the top, so a
  partially-finished batch redoes completed work unless the script skips existing
  outputs. Known limitation; most batch scripts are checkpointed. Not solved here.
- **No override needed:** a >600s foreground command is killed today regardless,
  so there is nothing to "let through." Zero regression; no escape hatch required.

## Testing

- **Unit (pure):** call `_build_local_redirect("python x.py", "/run/dir", 600)` and
  assert: `error` contains the redirect guidance and "submit_job";
  `suggested_action["tool"] == "submit_job"`;
  `suggested_action["args"]["scheduler"] == "local"`;
  `command` and `working_dir` round-trip; `working_dir` falls back to cwd when
  `cwd is None`. No real timeout needed.
- **Integration:** monkeypatch `_DEFAULT_TIMEOUT` to ~1s, run `sleep 3` through
  `run_shell`, assert the returned dict has `error_kind == "timeout"`, carries
  `suggested_action`, and that the call never raises.

## Acceptance criteria

1. A command exceeding the foreground limit returns a dict with `error_kind ==
   "timeout"`, an actionable `error` string, and a `suggested_action` naming
   `submit_job` with `scheduler="local"` and the original command.
2. Quick commands are byte-for-byte unaffected (success path unchanged).
3. `run_shell` still never raises on any path.
4. Both tests above pass.

## Follow-ups (out of scope here)

- **Step 2 — memory-recall gate, two halves:**
  - *Read side:* a gate inside `submit_job` that queries memory from `tool` + key
    args before launch and surfaces past pitfalls — soft-inject or hard-gate per
    the routing table. This is the "don't repeat mistakes" payoff; it generalizes
    the existing mechanical `apply_tool_memory_floor` to *learned* pitfalls.
  - *Write side (prerequisite for the read side to see local work):* bring
    `scheduler="local"` jobs into the run-record lifecycle — create a
    `.magnolia/runs/` record, have the poller scan them, and run
    completion→assess→memory-log / failure-capture so local outcomes become
    learnings. Without this, step 2's recall has a blind spot for exactly the
    local long-runs that step 1 routes into `submit_job`.
- Optional proactive hint for *repeatedly* known-long workloads, only if the
  10-min-then-relaunch wait proves annoying in practice.
