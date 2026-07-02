# Case-based run recall (step 2a-iii) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At submit, surface a pointer to the most *similar* past run of the same tool (ranked by agent-supplied system tags) so the agent can model its config on the right analog before launching.

**Architecture:** Index-don't-copy. Runs are recorded with `system_tags` + a run-dir pointer; a new `similar_runs` recall ranks past runs by tag overlap; the shipped `recall_gate` is extended to hold (once, cleared by `acknowledge=True`) when a similar prior run exists, unified with the existing warning hold. Remote-first (local runs enter the index once 2b lands).

**Tech Stack:** Python 3, pytest. Two FastMCP servers (compchem-memory, compchem-tools) in one venv; cross-package import already used.

## Global Constraints

- **Fail-open:** any recall/similarity error, missing project_dir, or falsy tool/tags → no hold, submission proceeds. `recall_gate` MUST NOT raise.
- **Index, don't copy:** the run record stores `{tool, status, run_dir, system_tags}` — NOT config params. `run_dir` is `remote.local_run_dir`.
- **Similarity = tag overlap** (case-insensitive) with agent-supplied `system_tags`; only successful runs (`status ∈ {pass, success, warning}`); zero-overlap and `None`/`fail`/`failed`/in-flight excluded; ranked by overlap then `date` desc; capped at `limit` (default 3).
- **Hold is one-shot,** cleared by the existing `acknowledge=True`; hold-dict has `held: True` (distinguishable from failure). At least one of `pitfalls`/`similar_runs` non-empty on any hold.
- **Additive only:** `record_run`'s new `system_tags` defaults to `[]`; absent arg must not change existing behavior. `recall_gate`'s new `system_tags` defaults to `None`.
- **No session suppression, no local-record creation, no auto-tags/semantic** — out of scope (follow-ups).
- Memory tests run from `mcp-servers/compchem-memory`; tools tests from `mcp-servers/compchem-tools`.

---

## File Structure

- **Modify:** `compchem-memory/.../tiers/project.py` — `record_run` gains `system_tags`; add `ProjectManager.similar_runs`.
- **Modify:** `compchem-memory/.../recall.py` — add module-level `similar_runs` wrapper.
- **Modify:** `compchem-tools/.../tools/recall_gate.py` — add `system_tags` param + similar-run hold branch.
- **Modify:** `compchem-tools/.../tools/jobs.py` — `submit_job` gains `system_tags`; pass to `recall_gate` and to `ssh_slurm.submit`.
- **Modify:** `compchem-tools/.../tools/ssh_slurm.py` — `submit` gains `system_tags`; pass to `record_run`.
- **Modify:** `compchem-tools/.../server.py` — MCP `submit_job` wrapper gains `system_tags`, forwarded.
- **Tests:** `compchem-memory/tests/test_similar_runs.py`; extend `compchem-tools/tests/test_recall_gate.py` and `test_recall_gate_wiring.py`.

Four tasks: (1) record `system_tags`; (2) `similar_runs` recall; (3) extend the gate; (4) thread `system_tags` through submit. Each independently testable.

---

### Task 1: record `system_tags` on run records

**Files:**
- Modify: `compchem-memory/src/compchem_memory/tiers/project.py` (`record_run`, ~line 475)
- Test: `compchem-memory/tests/test_similar_runs.py` (new file; first test)

**Interfaces:**
- Produces: `record_run(..., *, lifecycle=None, remote=None, system_tags: list[str] | None = None)` — persists a top-level `system_tags` field (default `[]`).

- [ ] **Step 1: Write the failing test**

Create `compchem-memory/tests/test_similar_runs.py`:

```python
import tempfile
from pathlib import Path
import pytest
import yaml
from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        pd = Path(d) / "project"
        pd.mkdir()
        yield pd


def test_record_run_persists_system_tags(project_dir):
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    path = mgr.record_run(pd, run_id="r1", tool="haddock3", status="pass",
                          system_tags=["peptide", "6mer"])
    rec = yaml.safe_load(Path(path).read_text())
    assert rec["system_tags"] == ["peptide", "6mer"]


def test_record_run_system_tags_defaults_empty(project_dir):
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    path = mgr.record_run(pd, run_id="r2", tool="haddock3", status="pass")
    rec = yaml.safe_load(Path(path).read_text())
    assert rec["system_tags"] == []
```

- [ ] **Step 2: Run to verify fail**

Run: `cd mcp-servers/compchem-memory && python -m pytest tests/test_similar_runs.py -v`
Expected: FAIL — `record_run() got an unexpected keyword argument 'system_tags'`.

- [ ] **Step 3: Implement**

In `tiers/project.py`, add the param and field to `record_run`:

Signature — add after `remote`:
```python
        remote: dict[str, Any] | None = None,
        system_tags: list[str] | None = None,
    ) -> str:
```
Record dict — add `system_tags` alongside the other top-level fields:
```python
        record: dict[str, Any] = {
            "run_id": run_id,
            "tool": tool,
            "status": status,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "system_tags": system_tags or [],
            "metrics": metrics or {},
            "quality_flags": quality_flags or [],
            "errors_solved": errors_solved or [],
        }
```

- [ ] **Step 4: Run to verify pass**

Run: `cd mcp-servers/compchem-memory && python -m pytest tests/test_similar_runs.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/compchem-memory/src/compchem_memory/tiers/project.py \
        mcp-servers/compchem-memory/tests/test_similar_runs.py
git commit -m "feat(compchem-memory): record_run persists system_tags"
```

---

### Task 2: `similar_runs` recall

**Files:**
- Modify: `compchem-memory/src/compchem_memory/tiers/project.py` (add method near `get_run_history`, ~line 668)
- Modify: `compchem-memory/src/compchem_memory/recall.py` (add wrapper)
- Test: `compchem-memory/tests/test_similar_runs.py` (append)

**Interfaces:**
- Consumes: `self.get_run_history(project_dir)` (returns list of full records incl. `tool`, `status`, `date`, `remote.local_run_dir`, `system_tags`); `record_run(..., system_tags=)` from Task 1; `compchem_memory.recall` GLOBAL_BASE + resolve helpers (from the shipped recall.py).
- Produces:
  - `ProjectManager.similar_runs(self, project_dir, tool, system_tags, limit=3) -> list[dict]`
  - `compchem_memory.recall.similar_runs(tool, system_tags, project_dir=None, limit=3) -> list[dict]`
  - Result dicts: `{run_id, run_dir, system_tags, date, status, score}`.

- [ ] **Step 1: Write the failing test**

Append to `compchem-memory/tests/test_similar_runs.py`:

```python
def _seed(mgr, pd, run_id, tool, status, tags, local_run_dir, date):
    mgr.record_run(pd, run_id=run_id, tool=tool, status=status,
                   system_tags=tags,
                   remote={"local_run_dir": local_run_dir})
    # normalize the date so ordering is deterministic
    import yaml as _y
    p = mgr._runs_dir(pd)
    for f in p.glob(f"*{run_id}.yaml"):
        rec = _y.safe_load(f.read_text()); rec["date"] = date
        f.write_text(_y.dump(rec, sort_keys=False))


def test_similar_runs_ranks_by_tag_overlap(project_dir):
    mgr = ProjectManager(project_dir); pd = str(project_dir)
    _seed(mgr, pd, "a", "haddock3", "pass", ["peptide", "6mer", "hsc70"], "/runs/a", "2026-06-01")
    _seed(mgr, pd, "b", "haddock3", "pass", ["peptide", "14mer"], "/runs/b", "2026-06-02")
    _seed(mgr, pd, "c", "gromacs", "pass", ["peptide", "6mer"], "/runs/c", "2026-06-03")

    hits = mgr.similar_runs(pd, "haddock3", ["peptide", "6mer", "hsc70"], limit=3)

    assert [h["run_id"] for h in hits] == ["a", "b"]  # a overlap 3, b overlap 1; gromacs excluded
    assert hits[0]["run_dir"] == "/runs/a"
    assert hits[0]["score"] == 3


def test_similar_runs_excludes_failed_and_zero_overlap(project_dir):
    mgr = ProjectManager(project_dir); pd = str(project_dir)
    _seed(mgr, pd, "f", "haddock3", "fail", ["peptide", "6mer"], "/runs/f", "2026-06-01")
    _seed(mgr, pd, "z", "haddock3", "pass", ["protein", "dimer"], "/runs/z", "2026-06-02")
    _seed(mgr, pd, "n", "haddock3", None, ["peptide", "6mer"], "/runs/n", "2026-06-03")
    assert mgr.similar_runs(pd, "haddock3", ["peptide", "6mer"]) == []


def test_similar_runs_limit_and_falsy(project_dir):
    mgr = ProjectManager(project_dir); pd = str(project_dir)
    for i in range(5):
        _seed(mgr, pd, f"r{i}", "xtb", "pass", ["qm", "opt"], f"/runs/r{i}", f"2026-06-0{i+1}")
    assert len(mgr.similar_runs(pd, "xtb", ["qm", "opt"], limit=2)) == 2
    assert mgr.similar_runs(pd, "xtb", []) == []
    assert mgr.similar_runs(pd, "", ["qm"]) == []


def test_module_wrapper_similar_runs(project_dir, monkeypatch):
    from compchem_memory import recall
    monkeypatch.setattr(recall, "GLOBAL_BASE", project_dir)
    monkeypatch.setattr(recall, "resolve_project_dir", lambda pd, default=".": str(project_dir))
    monkeypatch.setattr(recall, "ensure_project_store", lambda pd: None)
    _seed(ProjectManager(project_dir), str(project_dir), "a", "haddock3", "pass",
          ["peptide", "6mer"], "/runs/a", "2026-06-01")
    out = recall.similar_runs("haddock3", ["peptide", "6mer"], str(project_dir))
    assert len(out) == 1 and out[0]["run_dir"] == "/runs/a"
    assert recall.similar_runs("haddock3", []) == []
```

- [ ] **Step 2: Run to verify fail**

Run: `cd mcp-servers/compchem-memory && python -m pytest tests/test_similar_runs.py -v`
Expected: FAIL — `AttributeError: 'ProjectManager' object has no attribute 'similar_runs'`.

- [ ] **Step 3: Implement the method**

In `tiers/project.py`, after `get_run_history`:

```python
    _SUCCESS_STATUS = ("pass", "success", "warning")

    def similar_runs(
        self, project_dir: str, tool: str, system_tags: list[str], limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Past successful runs of `tool` ranked by system-tag overlap. Index
        model: returns run-dir pointers, not config copies. Empty on falsy
        tool/tags or no overlap."""
        if not tool or not system_tags:
            return []
        want = {str(t).lower() for t in system_tags}
        tl = tool.lower()
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for rec in self.get_run_history(project_dir):
            if str(rec.get("tool", "")).lower() != tl:
                continue
            if rec.get("status") not in self._SUCCESS_STATUS:
                continue
            rec_tags = {str(t).lower() for t in (rec.get("system_tags") or [])}
            overlap = len(want & rec_tags)
            if overlap == 0:
                continue
            run_dir = (rec.get("remote") or {}).get("local_run_dir", "") or ""
            date = str(rec.get("date", "") or "")
            scored.append((overlap, date, {
                "run_id": rec.get("run_id"),
                "run_dir": run_dir,
                "system_tags": sorted(rec_tags),
                "date": date,
                "status": rec.get("status"),
                "score": overlap,
            }))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [d for _, _, d in scored[:limit]]
```

- [ ] **Step 4: Implement the wrapper**

In `recall.py`, add (mirroring `warnings_for_tool`):

```python
def similar_runs(
    tool: str, system_tags: list[str], project_dir: str | None = None, limit: int = 3,
) -> list[dict[str, Any]]:
    """Resolve store + manager, delegate to ProjectManager.similar_runs.
    Returns [] on falsy tool/tags."""
    if not tool or not system_tags:
        return []
    pd = resolve_project_dir(project_dir, os.environ.get("MAGNOLIA_PROJECT_DIR", "."))
    ensure_project_store(pd)
    return ProjectManager(GLOBAL_BASE).similar_runs(pd, tool, system_tags, limit=limit)
```

- [ ] **Step 5: Run to verify pass**

Run: `cd mcp-servers/compchem-memory && python -m pytest tests/test_similar_runs.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/compchem-memory/src/compchem_memory/tiers/project.py \
        mcp-servers/compchem-memory/src/compchem_memory/recall.py \
        mcp-servers/compchem-memory/tests/test_similar_runs.py
git commit -m "feat(compchem-memory): similar_runs — tag-overlap recall of past run dirs"
```

---

### Task 3: extend `recall_gate` with the similar-run hold

**Files:**
- Modify: `compchem-tools/src/compchem_tools/tools/recall_gate.py`
- Test: `compchem-tools/tests/test_recall_gate.py` (append)

**Interfaces:**
- Consumes: `compchem_memory.recall.similar_runs(tool, system_tags, project_dir)` (Task 2); existing `_warnings_for_tool`.
- Produces: `recall_gate(tool, command, project_dir, acknowledge, system_tags=None) -> dict | None`; hold-dict gains a `similar_runs` key.

- [ ] **Step 1: Write the failing test**

Append to `compchem-tools/tests/test_recall_gate.py`:

```python
def _sim():
    return {"run_id": "a", "run_dir": "/runs/a", "system_tags": ["peptide", "6mer"],
            "date": "2026-06-01", "status": "pass", "score": 2}


def test_similar_run_only_returns_hold(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    monkeypatch.setattr(rg_mod, "_similar_runs", lambda *a, **k: [_sim()])
    out = recall_gate("haddock3", "cmd", "/proj", False, system_tags=["peptide", "6mer"])
    assert out["held"] is True
    assert out["pitfalls"] == []
    assert out["similar_runs"][0]["run_dir"] == "/runs/a"
    assert "acknowledge=True" in out["instruction"]


def test_no_warnings_no_similar_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    monkeypatch.setattr(rg_mod, "_similar_runs", lambda *a, **k: [])
    assert recall_gate("haddock3", "cmd", "/proj", False, system_tags=["x"]) is None


def test_acknowledge_skips_similar(monkeypatch):
    monkeypatch.setattr(rg_mod, "_similar_runs", lambda *a, **k: [_sim()])
    assert recall_gate("haddock3", "cmd", "/proj", True, system_tags=["peptide"]) is None


def test_similar_runs_failure_is_fail_open(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    def boom(*a, **k):
        raise RuntimeError("history unreadable")
    monkeypatch.setattr(rg_mod, "_similar_runs", boom)
    assert recall_gate("haddock3", "cmd", "/proj", False, system_tags=["x"]) is None
```

- [ ] **Step 2: Run to verify fail**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate.py -v`
Expected: FAIL — `recall_gate() got an unexpected keyword argument 'system_tags'` / no `_similar_runs`.

- [ ] **Step 3: Implement**

Edit `recall_gate.py`. Add the indirection helper next to `_warnings_for_tool`:

```python
def _similar_runs(tool, system_tags, project_dir):
    from compchem_memory.recall import similar_runs
    return similar_runs(tool, system_tags, project_dir)
```

Replace `_INSTRUCTION` with one covering both, and rewrite `recall_gate`:

```python
_INSTRUCTION = (
    "Relevant context was found for this {tool} submission. If 'pitfalls' is "
    "non-empty, verify each applies and adjust parameters. If 'similar_runs' is "
    "non-empty, inspect the most similar past run's directory and model your "
    "configuration on it (its config/inputs are saved there). Then resubmit "
    "with acknowledge=True to proceed. Held before launch — no compute spent."
)


def recall_gate(
    tool: str | None, command: str, project_dir: str | None, acknowledge: bool,
    system_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Hold-dict if the submission should be held (tool warnings and/or a
    similar prior run), else None. Fail-open: never raises."""
    try:
        if acknowledge:
            return None
        if not tool:
            return None
        warnings = _warnings_for_tool(tool, project_dir)
        similar = _similar_runs(tool, system_tags, project_dir) if system_tags else []
        if not warnings and not similar:
            return None
        pitfalls = [
            {"title": h.get("title"), "type": h.get("type"),
             "provisional": h.get("provisional", False),
             "summary": h.get("summary", ""), "source": h.get("source", "")}
            for h in warnings
        ]
        return {
            "success": False,
            "held": True,
            "reason": "recall_gate",
            "tool": tool,
            "pitfalls": pitfalls,
            "similar_runs": similar,
            "instruction": _INSTRUCTION.format(tool=tool),
        }
    except Exception as e:  # fail-open
        log.warning("recall_gate failed open: %s", e)
        return None
```

(Note: `_warnings_for_tool` and `_similar_runs` are called only inside the try, so a memory failure in either path returns None; if one succeeds and the other raises, the whole gate fails open to None — acceptable for v1.)

- [ ] **Step 4: Run to verify pass**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate.py -v`
Expected: PASS — the 4 new tests plus the pre-existing `recall_gate` tests (the old warning-only tests still pass; they call without `system_tags`, which defaults to `None` → `similar=[]`).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/compchem-tools/src/compchem_tools/tools/recall_gate.py \
        mcp-servers/compchem-tools/tests/test_recall_gate.py
git commit -m "feat(compchem-tools): recall_gate holds on a similar prior run (system_tags)"
```

---

### Task 4: thread `system_tags` through submit

**Files:**
- Modify: `compchem-tools/src/compchem_tools/tools/jobs.py` (`submit_job` signature + gate call + ssh-slurm delegation)
- Modify: `compchem-tools/src/compchem_tools/tools/ssh_slurm.py` (`submit` signature + `record_run` call)
- Modify: `compchem-tools/src/compchem_tools/server.py` (MCP `submit_job` wrapper)
- Test: `compchem-tools/tests/test_recall_gate_wiring.py` (update existing monkeypatch signatures + add a case)

**Interfaces:**
- Consumes: `recall_gate(tool, command, project_dir, acknowledge, system_tags)` (Task 3); `ProjectManager.record_run(..., system_tags=)` (Task 1).
- Produces: `submit_job(..., acknowledge=False, system_tags=None)`; `ssh_slurm.submit(..., system_tags=None)`; server wrapper forwards `system_tags`.

- [ ] **Step 1: Update + add wiring tests**

In `compchem-tools/tests/test_recall_gate_wiring.py`, the existing `_force_hold` monkeypatches `jobs_mod.recall_gate` with a 4-arg lambda; **update its lambda to accept `system_tags`** and add a similar-run wiring test:

```python
def _force_hold(monkeypatch):
    monkeypatch.setattr(
        jobs_mod, "recall_gate",
        lambda tool, command, project_dir, acknowledge, system_tags=None:
            None if acknowledge else {"success": False, "held": True,
                                      "reason": "recall_gate", "tool": tool,
                                      "pitfalls": [], "similar_runs": [{"run_dir": "/runs/a"}],
                                      "instruction": "resubmit with acknowledge=True"},
    )


def test_system_tags_forwarded_and_held(monkeypatch, tmp_path):
    seen = {}
    def fake_gate(tool, command, project_dir, acknowledge, system_tags=None):
        seen["tags"] = system_tags
        return {"held": True, "success": False} if not acknowledge else None
    monkeypatch.setattr(jobs_mod, "recall_gate", fake_gate)
    called = {"local": False}
    monkeypatch.setattr(jobs_mod, "_submit_local",
                        lambda *a, **k: called.__setitem__("local", True) or {"success": True})
    out = submit_job("cmd", str(tmp_path), scheduler="local", tool="haddock3",
                     system_tags=["peptide", "6mer"])
    assert out["held"] is True
    assert called["local"] is False
    assert seen["tags"] == ["peptide", "6mer"]  # forwarded into the gate
```

(The other existing wiring tests call `_force_hold`; with the updated lambda they keep passing.)

- [ ] **Step 2: Run to verify fail**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate_wiring.py -v`
Expected: FAIL — `submit_job() got an unexpected keyword argument 'system_tags'`.

- [ ] **Step 3: Wire `jobs.py`**

Add `system_tags: list[str] | None = None` to `submit_job`'s signature (after `acknowledge`). Update the gate call:

```python
    held = recall_gate(tool, command, project_dir, acknowledge, system_tags)
    if held is not None:
        return held
```

In the `scheduler == "ssh-slurm"` branch, pass `system_tags=system_tags` into the `ssh_slurm.submit(...)` call.

- [ ] **Step 4: Wire `ssh_slurm.py`**

Add `system_tags: list[str] | None = None` to `submit`'s signature. In the `record_run(...)` call (the non-restart branch, ~line 302), pass `system_tags=system_tags`:

```python
        _PROJECT_MANAGER.record_run(
            project_dir=project_dir,
            run_id=run_id,
            tool=tool or "raw",
            status=None,
            lifecycle="submitting",
            remote=remote_fields,
            system_tags=system_tags,
        )
```

- [ ] **Step 5: Wire `server.py` wrapper**

Add `system_tags: list[str] | None = None` to the MCP `submit_job` wrapper signature (after `acknowledge`), forward it in the `_submit_job(...)` call as `system_tags=system_tags`, and add one docstring line:

```
    system_tags: short descriptors of the system (e.g. ["peptide","6mer","hsc70"])
      used to recall the most similar past run before launch.
```

- [ ] **Step 6: Run to verify pass (+ no regression)**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate_wiring.py tests/test_recall_gate.py tests/test_run_shell.py -v`
Expected: PASS — new + updated wiring tests, the gate tests, and the run_shell suite.

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/compchem-tools/src/compchem_tools/tools/jobs.py \
        mcp-servers/compchem-tools/src/compchem_tools/tools/ssh_slurm.py \
        mcp-servers/compchem-tools/src/compchem_tools/server.py \
        mcp-servers/compchem-tools/tests/test_recall_gate_wiring.py
git commit -m "feat(compchem-tools): thread system_tags through submit_job -> recall_gate + record_run"
```

---

## Self-Review

**Spec coverage:**
- Capture `system_tags` on the record → Task 1. ✓
- `similar_runs` tag-overlap recall returning run-dir pointers → Task 2. ✓
- `recall_gate` similar-run hold, unified with warnings, cleared by `acknowledge` → Task 3. ✓
- Thread `system_tags` submit_job → recall_gate + ssh_slurm.submit → record_run; server wrapper → Task 4. ✓
- Index-not-copy (run_dir pointer, no config copy) → Task 2 result shape. ✓
- Successful-only, zero-overlap/failed/in-flight excluded, ranked by overlap then date, limit → Task 2 tests. ✓
- Fail-open (gate never raises; similar_runs error → proceed) → Task 3 tests. ✓
- Additive (system_tags default [] / None; old warning-only tests still pass) → Tasks 1 & 3. ✓
- Held-does-not-launch; acknowledge launches; tags forwarded → Task 4 test. ✓
- Out-of-scope (session suppression, local records, semantic/auto-tags) → not in plan. ✓

**Placeholder scan:** No TBD/TODO; every code step complete.

**Type consistency:** `recall_gate(tool, command, project_dir, acknowledge, system_tags=None)` identical across Task 3 def, Task 4 call + monkeypatch lambdas, and tests. `similar_runs(tool, system_tags, project_dir=None, limit=3)` identical in Task 2 def and the gate's `_similar_runs` indirection. Result-dict keys (`run_id/run_dir/system_tags/date/status/score`) consistent across Task 2 producer, Task 3 hold payload, and assertions. `record_run(..., system_tags=None)` identical in Task 1 def and Task 4 call site.
