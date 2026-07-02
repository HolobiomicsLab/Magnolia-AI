# run_shell timeout → submit_job redirect — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a `run_shell` command exceeds its foreground timeout, return an actionable redirect to `submit_job(scheduler="local")` instead of a dead-end "timed out" error.

**Architecture:** A pure helper `_build_local_redirect` produces the redirect fields (an actionable `error` string + a structured `suggested_action`). The existing `TimeoutExpired` branch in `run_shell` merges those fields into its already-structured return dict. Schema is strictly additive; `error_kind` stays `"timeout"`; the never-raise contract is preserved.

**Tech Stack:** Python 3, pytest (with `monkeypatch`). MCP tool in the `compchem-tools` FastMCP server.

## Global Constraints

- `run_shell` MUST NOT raise on any path — every failure returns a structured dict with `error_kind`. (An escaping exception cascades through fastmcp's anyio loop and kills the whole MCP server + poller daemon.)
- Return schema changes must be **additive only** — keep `exit_code`, `stdout`, `stderr`, `error_kind` on the timeout path so existing consumers (and `tests/test_run_shell.py::test_timeout_returns_dict_does_not_raise`, which asserts `"600s" in out["error"]`) keep working.
- All work in one file: `mcp-servers/compchem-tools/src/compchem_tools/tools/shell.py`. No plugin, no new tool, no change to the agent-facing `run_shell` signature.
- Tests live in `mcp-servers/compchem-tools/tests/` (`testpaths = ["tests"]`). Run from the `compchem-tools` package dir.

---

## File Structure

- **Modify:** `mcp-servers/compchem-tools/src/compchem_tools/tools/shell.py`
  - Add module-level pure helper `_build_local_redirect`.
  - Edit the `except subprocess.TimeoutExpired` branch to merge the helper's output.
- **Modify (tests):** `mcp-servers/compchem-tools/tests/test_run_shell.py`
  - Add a pure unit test for `_build_local_redirect`.
  - Add a branch test asserting the timeout path now carries `suggested_action`.

Two tasks: the helper (self-contained, pure, unit-tested in isolation) and the wiring (changes observable run_shell behavior). A reviewer could accept one and reject the other, so they are split.

---

### Task 1: Pure redirect-builder helper

**Files:**
- Modify: `mcp-servers/compchem-tools/src/compchem_tools/tools/shell.py` (add helper after `_truncate`, ~line 28)
- Test: `mcp-servers/compchem-tools/tests/test_run_shell.py`

**Interfaces:**
- Consumes: nothing (uses stdlib `os`, already imported in `shell.py`).
- Produces: `_build_local_redirect(cmd: str, cwd: str | None, timeout: int) -> dict[str, Any]` returning a dict with exactly two keys: `"error"` (str) and `"suggested_action"` (dict shaped `{"tool": "submit_job", "args": {"command", "working_dir", "scheduler"}}`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_run_shell.py`:

```python
from compchem_tools.tools.shell import _build_local_redirect


def test_build_local_redirect_shape_and_content():
    out = _build_local_redirect("python analyze.py", "/runs/dock1", 600)
    # actionable error mentions the limit and the right tool, and warns off re-running
    assert "600s" in out["error"]
    assert "submit_job" in out["error"]
    assert "run_shell" in out["error"]  # the "do NOT re-run via run_shell" warning
    # structured, ready-to-issue call
    sa = out["suggested_action"]
    assert sa["tool"] == "submit_job"
    assert sa["args"]["command"] == "python analyze.py"
    assert sa["args"]["working_dir"] == "/runs/dock1"
    assert sa["args"]["scheduler"] == "local"
    # ncores/memory deliberately omitted — agent sets them per workload
    assert "ncores" not in sa["args"]


def test_build_local_redirect_cwd_none_falls_back_to_getcwd(monkeypatch):
    monkeypatch.setattr("compchem_tools.tools.shell.os.getcwd", lambda: "/here")
    out = _build_local_redirect("ls", None, 600)
    assert out["suggested_action"]["args"]["working_dir"] == "/here"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_run_shell.py::test_build_local_redirect_shape_and_content -v`
Expected: FAIL with `ImportError: cannot import name '_build_local_redirect'`

- [ ] **Step 3: Write minimal implementation**

In `shell.py`, after the `_truncate` function (before `run_shell`), add:

```python
def _build_local_redirect(cmd: str, cwd: str | None, timeout: int) -> dict[str, Any]:
    """Fields to merge into a timeout result that redirect a long foreground
    command to submit_job(scheduler="local").

    Pure apart from the os.getcwd() fallback when cwd is None. `timeout` is
    passed in (not read from the module constant) so the message and tests stay
    in lockstep under monkeypatch.
    """
    working_dir = cwd or os.getcwd()
    return {
        "error": (
            f"command exceeded the {timeout}s foreground limit. Long runs — "
            "including batches of many small jobs, e.g. a docking loop — must "
            'run in the background via submit_job(scheduler="local"). Do NOT '
            "re-run this via run_shell; it will time out again. Relaunch using "
            "the suggested_action below; set ncores/memory appropriate to the "
            "workload."
        ),
        "suggested_action": {
            "tool": "submit_job",
            "args": {
                "command": cmd,
                "working_dir": working_dir,
                "scheduler": "local",
            },
        },
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_run_shell.py::test_build_local_redirect_shape_and_content tests/test_run_shell.py::test_build_local_redirect_cwd_none_falls_back_to_getcwd -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/compchem-tools/src/compchem_tools/tools/shell.py mcp-servers/compchem-tools/tests/test_run_shell.py
git commit -m "feat(compchem-tools): add _build_local_redirect helper for run_shell timeouts"
```

---

### Task 2: Wire the redirect into the timeout branch

**Files:**
- Modify: `mcp-servers/compchem-tools/src/compchem_tools/tools/shell.py:71-78` (the `except subprocess.TimeoutExpired` branch)
- Test: `mcp-servers/compchem-tools/tests/test_run_shell.py`

**Interfaces:**
- Consumes: `_build_local_redirect(cmd, cwd, timeout)` from Task 1; module constant `_DEFAULT_TIMEOUT` (== 600); `_truncate` (existing).
- Produces: on timeout, `run_shell` returns `{"exit_code": -1, "stdout": <tail>, "stderr": <tail>, "error_kind": "timeout", "error": <actionable>, "suggested_action": {...}}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_run_shell.py` (mirrors the existing `test_timeout_returns_dict_does_not_raise` style):

```python
def test_timeout_returns_submit_job_redirect(monkeypatch):
    """On timeout, run_shell must redirect to submit_job(scheduler=local)
    while preserving the timeout schema and never raising."""
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=600, output="partial", stderr="")
    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)

    out = run_shell("python big_analysis.py", cwd="/runs/x")

    # schema preserved
    assert out["exit_code"] == -1
    assert out["error_kind"] == "timeout"
    assert out["stdout"] == "partial"
    assert "600s" in out["error"]  # keeps existing consumers happy
    # new redirect
    sa = out["suggested_action"]
    assert sa["tool"] == "submit_job"
    assert sa["args"]["command"] == "python big_analysis.py"
    assert sa["args"]["working_dir"] == "/runs/x"
    assert sa["args"]["scheduler"] == "local"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_run_shell.py::test_timeout_returns_submit_job_redirect -v`
Expected: FAIL with `KeyError: 'suggested_action'`

- [ ] **Step 3: Write minimal implementation**

In `shell.py`, replace the existing timeout branch (lines 71-78):

```python
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": -1,
            "stdout": _truncate(e.stdout),
            "stderr": _truncate(e.stderr),
            "error_kind": "timeout",
            "error": f"command timed out after {_DEFAULT_TIMEOUT}s",
        }
```

with:

```python
    except subprocess.TimeoutExpired as e:
        result = {
            "exit_code": -1,
            "stdout": _truncate(e.stdout),
            "stderr": _truncate(e.stderr),
            "error_kind": "timeout",
        }
        # Turn the dead-end timeout into an actionable redirect: long runs
        # belong in the background via submit_job(scheduler="local").
        result.update(_build_local_redirect(cmd, cwd, _DEFAULT_TIMEOUT))
        return result
```

(`error` is now supplied by `_build_local_redirect` and still contains `"600s"`.)

- [ ] **Step 4: Run the full file's tests to verify pass + no regression**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_run_shell.py -v`
Expected: PASS — the new test plus all pre-existing ones, including `test_timeout_returns_dict_does_not_raise` (still green because `"600s"` remains in `error`).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/compchem-tools/src/compchem_tools/tools/shell.py mcp-servers/compchem-tools/tests/test_run_shell.py
git commit -m "feat(compchem-tools): run_shell timeout redirects to submit_job(scheduler=local)"
```

---

## Self-Review

**Spec coverage:**
- Reactive timeout→redirect on the `run_shell` `TimeoutExpired` branch → Task 2. ✓
- Pure `_build_local_redirect(cmd, cwd, timeout)` helper → Task 1. ✓
- Additive schema, `error_kind` stays `"timeout"`, never-raise preserved → Task 2 (asserted in test). ✓
- `suggested_action` names `submit_job`, `scheduler="local"`, original command, cwd fallback, ncores/memory omitted → Tasks 1 & 2. ✓
- Tests: pure unit test of the helper + integration of the timeout branch → both tasks. ✓
- Loop-safety ("do NOT re-run via run_shell") carried in the `error` string → Task 1 (asserted). ✓
- Out of scope (memory gate, local lifecycle, plugin, prediction) → not in plan. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step has complete code. ✓

**Type consistency:** `_build_local_redirect(cmd, cwd, timeout)` signature identical in Task 1 definition, Task 2 call, and both tests; `suggested_action` key names (`tool`/`args`/`command`/`working_dir`/`scheduler`) identical across helper, wiring, and assertions. ✓
