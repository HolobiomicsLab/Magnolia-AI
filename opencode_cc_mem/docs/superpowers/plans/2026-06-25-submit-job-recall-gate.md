# submit_job recall gate (step 2a) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before `submit_job` launches, recall tool-scoped past pitfalls from memory; if any exist and the caller hasn't acknowledged, hold the submission (return without launching) so the agent can review and resubmit.

**Architecture:** Three units across the two packages. `compchem_memory` gains a keyword-free, tool-indexed retrieval (`ProjectManager.select_warnings_for_tool` + a thin module-level `warnings_for_tool` wrapper). `compchem_tools` gains a fail-open `recall_gate` that calls the wrapper and returns a hold-dict, wired into `submit_job` right after `apply_tool_memory_floor`. v1 surfaces pitfalls and lets the agent decide via an `acknowledge` arg; no auto-apply.

**Tech Stack:** Python 3, pytest. Two FastMCP servers (`compchem-memory`, `compchem-tools`) installed in one venv; cross-package import is already used (`poller.py` imports `compchem_memory.learning.orchestrator`).

## Global Constraints

- **Fail-open everywhere:** any recall error, missing/unresolvable `project_dir`, or `tool=None` must let the submission proceed unchanged. `recall_gate` MUST NOT raise.
- **Hold is distinguishable from failure:** a hold returns `{"success": False, "held": True, "reason": "recall_gate", ...}`. A real submission failure has no `held` key.
- **`acknowledge=True` bypasses the gate** — the agent's resubmission is the decision. Default `acknowledge=False`.
- **Recall = tool-scoped warnings:** only `failure_pattern` / `error_resolution` entries whose frontmatter `tools` (case-insensitive) include the submission's `tool`; promoted + staging; staging flagged `provisional=True`; capped at `limit=5`; ordered promoted-before-staging.
- **No auto-apply, no structured-rule registry, no `surfaced_in_session`, no write-side lifecycle** — those are out of scope (2a-ii / 2b).
- Memory tests run from `mcp-servers/compchem-memory`; tools tests from `mcp-servers/compchem-tools` (`testpaths = ["tests"]` in each).

---

## File Structure

- **Create:** `mcp-servers/compchem-memory/src/compchem_memory/recall.py` — module-level `warnings_for_tool` wrapper (the single entry point the gate imports).
- **Modify:** `mcp-servers/compchem-memory/src/compchem_memory/tiers/project.py` — add `ProjectManager.select_warnings_for_tool`.
- **Create:** `mcp-servers/compchem-tools/src/compchem_tools/tools/recall_gate.py` — `recall_gate`.
- **Modify:** `mcp-servers/compchem-tools/src/compchem_tools/tools/jobs.py` — add `acknowledge` param + call the gate.
- **Modify:** `mcp-servers/compchem-tools/src/compchem_tools/server.py` — add `acknowledge` to the MCP `submit_job` wrapper, pass through.
- **Tests:** `mcp-servers/compchem-memory/tests/test_recall.py`, `mcp-servers/compchem-tools/tests/test_recall_gate.py`, and additions to `mcp-servers/compchem-tools/tests/` for the wiring.

Three tasks: (1) memory-side retrieval, (2) the gate, (3) the wiring. Each is independently testable and a reviewer could accept one while rejecting another.

---

### Task 1: Tool-scoped warning retrieval (compchem_memory)

**Files:**
- Modify: `mcp-servers/compchem-memory/src/compchem_memory/tiers/project.py` (add a method beside `search_staging`)
- Create: `mcp-servers/compchem-memory/src/compchem_memory/recall.py`
- Test: `mcp-servers/compchem-memory/tests/test_recall.py`

**Interfaces:**
- Consumes: existing `ProjectManager` internals — `self._entries_dir(project_dir)`, `self._staging_dir(project_dir)`, `self._parse_frontmatter(text)`; `compchem_memory.storage.resolve_project_dir`, `ensure_project_store`. `create_entry(project_dir, title, content, tags=, source=, staging=, entry_type=, tools=, confidence=)` for test seeding. Frontmatter fields written by `create_entry`: `type`, `title`, `description` (=content[:200]), `tools`, `source`, `date`.
- Produces:
  - `ProjectManager.select_warnings_for_tool(self, project_dir: str, tool: str, limit: int = 5) -> list[dict[str, Any]]`
  - `compchem_memory.recall.warnings_for_tool(tool: str, project_dir: str | None = None, limit: int = 5) -> list[dict[str, Any]]`
  - Each result dict has keys: `title`, `type`, `provisional` (bool), `summary` (str), `source` (str), `path` (str).

- [ ] **Step 1: Write the failing test**

Create `mcp-servers/compchem-memory/tests/test_recall.py`:

```python
import pytest
from pathlib import Path
import tempfile

from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        pd = Path(d) / "project"
        pd.mkdir()
        yield pd


def _mgr(project_dir):
    # tests construct ProjectManager with the project dir as global_base
    return ProjectManager(project_dir)


def test_returns_failure_entries_for_tool(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "haddock3 OOM on Azzurra", "32 CNS workers exceeded 32GB",
                     entry_type="failure_pattern", tools=["haddock3"])
    mgr.create_entry(pd, "gromacs box too small", "pbc errors",
                     entry_type="error_resolution", tools=["gromacs"])
    mgr.create_entry(pd, "a plain note about haddock3", "not a warning",
                     entry_type="note", tools=["haddock3"])

    hits = mgr.select_warnings_for_tool(pd, "haddock3")

    assert len(hits) == 1
    assert hits[0]["title"] == "haddock3 OOM on Azzurra"
    assert hits[0]["type"] == "failure_pattern"
    assert hits[0]["provisional"] is False
    assert "32GB" in hits[0]["summary"]  # from frontmatter description (content[:200])


def test_tool_match_is_case_insensitive_and_excludes_other_tools(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "w", "x", entry_type="failure_pattern", tools=["HADDOCK3"])
    hits = mgr.select_warnings_for_tool(pd, "haddock3")
    assert len(hits) == 1


def test_staging_hits_flagged_provisional_and_ordered_after_promoted(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "promoted warn", "p", entry_type="failure_pattern", tools=["qm"])
    mgr.create_entry(pd, "staging warn", "s", entry_type="failure_pattern", tools=["qm"],
                     staging=True)
    hits = mgr.select_warnings_for_tool(pd, "qm")
    assert [h["provisional"] for h in hits] == [False, True]  # promoted first


def test_limit_respected(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    for i in range(7):
        mgr.create_entry(pd, f"warn {i}", "c", entry_type="failure_pattern", tools=["p2rank"])
    assert len(mgr.select_warnings_for_tool(pd, "p2rank", limit=3)) == 3


def test_empty_when_no_match_or_falsy_tool(project_dir):
    mgr = _mgr(project_dir)
    pd = str(project_dir)
    mgr.create_entry(pd, "w", "c", entry_type="failure_pattern", tools=["haddock3"])
    assert mgr.select_warnings_for_tool(pd, "gnina") == []
    assert mgr.select_warnings_for_tool(pd, "") == []


def test_module_wrapper_returns_plain_list(project_dir, monkeypatch):
    # warnings_for_tool resolves store + manager and returns a list (not JSON)
    from compchem_memory import recall
    monkeypatch.setattr(recall, "GLOBAL_BASE", project_dir)
    monkeypatch.setattr(recall, "resolve_project_dir", lambda pd, default=".": str(project_dir))
    monkeypatch.setattr(recall, "ensure_project_store", lambda pd: None)
    ProjectManager(project_dir).create_entry(
        str(project_dir), "w", "c", entry_type="failure_pattern", tools=["haddock3"])
    out = recall.warnings_for_tool("haddock3", str(project_dir))
    assert isinstance(out, list) and len(out) == 1
    assert recall.warnings_for_tool("") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/compchem-memory && python -m pytest tests/test_recall.py -v`
Expected: FAIL — `AttributeError: 'ProjectManager' object has no attribute 'select_warnings_for_tool'` and `ModuleNotFoundError: compchem_memory.recall`.

- [ ] **Step 3: Add the `ProjectManager` method**

In `tiers/project.py`, add immediately after `search_staging` (around line 274):

```python
    _WARNING_TYPES = ("failure_pattern", "error_resolution")

    def select_warnings_for_tool(
        self, project_dir: str, tool: str, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Keyword-free, tool-indexed recall of warning entries for a tool.

        Returns failure_pattern / error_resolution entries whose frontmatter
        `tools` includes `tool` (case-insensitive), from promoted entries AND
        staging. Staging hits are flagged provisional=True. Ordered
        promoted-before-staging, then by `date` descending. Capped at `limit`.
        """
        if not tool:
            return []
        tl = tool.lower()

        def _scan(directory, provisional):
            out = []
            if not directory.exists():
                return out
            for f in sorted(directory.glob("*.md")):
                if f.name == "INDEX.md":
                    continue
                try:
                    meta = self._parse_frontmatter(
                        f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if meta.get("type") not in self._WARNING_TYPES:
                    continue
                entry_tools = [str(t).lower() for t in meta.get("tools", [])]
                if tl not in entry_tools:
                    continue
                out.append({
                    "title": meta.get("title", f.stem),
                    "type": meta.get("type"),
                    "provisional": provisional,
                    "summary": meta.get("description", "") or meta.get("title", ""),
                    "source": meta.get("source", "") or f.name,
                    "path": str(f),
                    "date": meta.get("date", ""),
                })
            return out

        promoted = _scan(self._entries_dir(project_dir), False)
        staging = _scan(self._staging_dir(project_dir), True)
        promoted.sort(key=lambda e: e.get("date", ""), reverse=True)
        staging.sort(key=lambda e: e.get("date", ""), reverse=True)
        return (promoted + staging)[:limit]
```

- [ ] **Step 4: Create the module wrapper**

Create `mcp-servers/compchem-memory/src/compchem_memory/recall.py`:

```python
"""Tool-scoped warning recall — the entry point the submit_job recall gate imports.

Kept separate from server.py so importers (compchem_tools) don't pull in the
whole FastMCP server. Returns plain Python lists, not JSON strings.
"""
import os
from pathlib import Path
from typing import Any

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.storage import resolve_project_dir, ensure_project_store

GLOBAL_BASE = Path(os.path.expanduser("~/.magnolia"))


def warnings_for_tool(
    tool: str, project_dir: str | None = None, limit: int = 5,
) -> list[dict[str, Any]]:
    """Resolve the project store + ProjectManager, then delegate to
    ProjectManager.select_warnings_for_tool. Returns [] on falsy tool."""
    if not tool:
        return []
    pd = resolve_project_dir(project_dir, os.environ.get("MAGNOLIA_PROJECT_DIR", "."))
    ensure_project_store(pd)
    mgr = ProjectManager(GLOBAL_BASE)
    return mgr.select_warnings_for_tool(pd, tool, limit=limit)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd mcp-servers/compchem-memory && python -m pytest tests/test_recall.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/compchem-memory/src/compchem_memory/tiers/project.py \
        mcp-servers/compchem-memory/src/compchem_memory/recall.py \
        mcp-servers/compchem-memory/tests/test_recall.py
git commit -m "feat(compchem-memory): tool-scoped warning recall (select_warnings_for_tool + warnings_for_tool)"
```

---

### Task 2: The recall gate (compchem_tools)

**Files:**
- Create: `mcp-servers/compchem-tools/src/compchem_tools/tools/recall_gate.py`
- Test: `mcp-servers/compchem-tools/tests/test_recall_gate.py`

**Interfaces:**
- Consumes: `compchem_memory.recall.warnings_for_tool(tool, project_dir, limit)` from Task 1 (returns list of dicts with `title`/`type`/`provisional`/`summary`/`source`).
- Produces: `recall_gate(tool: str | None, command: str, project_dir: str | None, acknowledge: bool) -> dict[str, Any] | None`. Returns `None` to proceed, or a hold-dict `{"success": False, "held": True, "reason": "recall_gate", "tool", "pitfalls": [...], "instruction"}`. Never raises.

- [ ] **Step 1: Write the failing test**

Create `mcp-servers/compchem-tools/tests/test_recall_gate.py`:

```python
from compchem_tools.tools import recall_gate as rg_mod
from compchem_tools.tools.recall_gate import recall_gate


def _hit():
    return {"title": "haddock3 OOM", "type": "failure_pattern",
            "provisional": False, "summary": "32 workers > 32GB", "source": "e1"}


def test_acknowledge_true_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [_hit()])
    assert recall_gate("haddock3", "cmd", "/proj", True) is None


def test_no_hits_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [])
    assert recall_gate("haddock3", "cmd", "/proj", False) is None


def test_hits_return_hold_dict(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [_hit()])
    out = recall_gate("haddock3", "cmd", "/proj", False)
    assert out["success"] is False
    assert out["held"] is True
    assert out["reason"] == "recall_gate"
    assert out["tool"] == "haddock3"
    assert out["pitfalls"][0]["title"] == "haddock3 OOM"
    assert out["pitfalls"][0]["provisional"] is False
    assert "acknowledge=True" in out["instruction"]


def test_tool_none_proceeds(monkeypatch):
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", lambda *a, **k: [_hit()])
    assert recall_gate(None, "cmd", "/proj", False) is None


def test_fail_open_on_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("memory down")
    monkeypatch.setattr(rg_mod, "_warnings_for_tool", boom)
    assert recall_gate("haddock3", "cmd", "/proj", False) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: compchem_tools.tools.recall_gate`.

- [ ] **Step 3: Write the implementation**

Create `mcp-servers/compchem-tools/src/compchem_tools/tools/recall_gate.py`:

```python
"""Pre-launch recall gate for submit_job. Fail-open: never raises, never blocks
on a memory problem. v1 surfaces tool-scoped pitfalls and holds the submission
until the caller resubmits with acknowledge=True."""
import logging
from typing import Any

log = logging.getLogger(__name__)


def _warnings_for_tool(tool, project_dir):
    # Indirection so tests can monkeypatch without importing compchem_memory.
    from compchem_memory.recall import warnings_for_tool
    return warnings_for_tool(tool, project_dir)


_INSTRUCTION = (
    "Relevant past pitfalls were found for this {tool} submission (see "
    "'pitfalls'). Verify whether each applies to THIS submission; adjust "
    "parameters if needed; then resubmit with acknowledge=True to proceed. "
    "The job was held before launch — no compute was spent."
)


def recall_gate(
    tool: str | None, command: str, project_dir: str | None, acknowledge: bool,
) -> dict[str, Any] | None:
    """Return a hold-dict if the submission should be held, else None to proceed."""
    try:
        if acknowledge:
            return None
        if not tool:
            return None
        hits = _warnings_for_tool(tool, project_dir)
        if not hits:
            return None
        pitfalls = [
            {"title": h.get("title"), "type": h.get("type"),
             "provisional": h.get("provisional", False),
             "summary": h.get("summary", ""), "source": h.get("source", "")}
            for h in hits
        ]
        return {
            "success": False,
            "held": True,
            "reason": "recall_gate",
            "tool": tool,
            "pitfalls": pitfalls,
            "instruction": _INSTRUCTION.format(tool=tool),
        }
    except Exception as e:  # fail-open: a memory glitch must never block a submit
        log.warning("recall_gate failed open: %s", e)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/compchem-tools/src/compchem_tools/tools/recall_gate.py \
        mcp-servers/compchem-tools/tests/test_recall_gate.py
git commit -m "feat(compchem-tools): add recall_gate (fail-open pre-launch memory hold)"
```

---

### Task 3: Wire the gate into submit_job

**Files:**
- Modify: `mcp-servers/compchem-tools/src/compchem_tools/tools/jobs.py` (the `submit_job` signature + body, around lines 27-50)
- Modify: `mcp-servers/compchem-tools/src/compchem_tools/server.py` (the MCP `submit_job` wrapper, around line 534)
- Test: `mcp-servers/compchem-tools/tests/test_recall_gate_wiring.py`

**Interfaces:**
- Consumes: `recall_gate(tool, command, project_dir, acknowledge)` from Task 2; existing `submit_job` and `_submit_local`/`_submit_slurm` in `jobs.py`.
- Produces: `submit_job(..., acknowledge: bool = False)` that returns the hold-dict (and does not launch) when the gate holds; the `server.py` wrapper forwards `acknowledge`.

- [ ] **Step 1: Write the failing test**

Create `mcp-servers/compchem-tools/tests/test_recall_gate_wiring.py`:

```python
from compchem_tools.tools import jobs as jobs_mod
from compchem_tools.tools.jobs import submit_job


def _force_hold(monkeypatch):
    monkeypatch.setattr(
        jobs_mod, "recall_gate",
        lambda tool, command, project_dir, acknowledge:
            None if acknowledge else {"success": False, "held": True,
                                       "reason": "recall_gate", "tool": tool,
                                       "pitfalls": [{"title": "x"}],
                                       "instruction": "resubmit with acknowledge=True"},
    )


def test_unacknowledged_submit_is_held_and_does_not_launch(monkeypatch, tmp_path):
    _force_hold(monkeypatch)
    called = {"local": False}
    monkeypatch.setattr(jobs_mod, "_submit_local",
                        lambda *a, **k: called.__setitem__("local", True) or {"success": True})
    out = submit_job("python x.py", str(tmp_path), scheduler="local", tool="haddock3")
    assert out["held"] is True
    assert called["local"] is False  # launcher never invoked


def test_acknowledged_submit_launches(monkeypatch, tmp_path):
    _force_hold(monkeypatch)
    called = {"local": False}
    monkeypatch.setattr(jobs_mod, "_submit_local",
                        lambda *a, **k: called.__setitem__("local", True) or
                        {"success": True, "job_id": "local_1_abc", "scheduler": "local"})
    out = submit_job("python x.py", str(tmp_path), scheduler="local",
                     tool="haddock3", acknowledge=True)
    assert called["local"] is True
    assert out.get("success") is True


def test_gate_failure_does_not_block(monkeypatch, tmp_path):
    # recall_gate returning None (its fail-open contract) => normal launch
    monkeypatch.setattr(jobs_mod, "recall_gate", lambda *a, **k: None)
    called = {"local": False}
    monkeypatch.setattr(jobs_mod, "_submit_local",
                        lambda *a, **k: called.__setitem__("local", True) or
                        {"success": True, "job_id": "local_1_abc", "scheduler": "local"})
    submit_job("python x.py", str(tmp_path), scheduler="local", tool="haddock3")
    assert called["local"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate_wiring.py -v`
Expected: FAIL — `submit_job() got an unexpected keyword argument 'acknowledge'`.

- [ ] **Step 3: Wire the gate into `jobs.py`**

In `tools/jobs.py`, add `acknowledge: bool = False` to the `submit_job` signature (after `remote_precommand`), add the import at top of the module (with the other imports), and insert the gate call right after the `apply_tool_memory_floor` line.

Add near the top-of-file imports:

```python
from compchem_tools.tools.recall_gate import recall_gate
```

Signature — add the parameter:

```python
    restart_of: str | None = None,
    remote_precommand: str | None = None,
    acknowledge: bool = False,
) -> dict[str, Any]:
```

Body — immediately after `memory = apply_tool_memory_floor(tool, ncores, memory)`:

```python
    # Pre-launch recall gate: hold once if memory has tool-scoped pitfalls,
    # unless the caller has acknowledged. Fail-open (recall_gate never raises).
    held = recall_gate(tool, command, project_dir, acknowledge)
    if held is not None:
        return held
```

- [ ] **Step 4: Forward `acknowledge` from the server wrapper**

In `server.py`, the MCP `submit_job` wrapper: add `acknowledge: bool = False` to its signature (after `remote_precommand`) and pass `acknowledge=acknowledge` into the `_submit_job(...)` call.

Signature:

```python
    restart_of: str | None = None,
    remote_precommand: str | None = None,
    acknowledge: bool = False,
) -> str:
```

Call:

```python
    result = _submit_job(
        command, working_dir, scheduler, job_name, ncores, memory, time_limit, partition,
        project_dir=project_dir, cluster=cluster, account=account, qos=qos, tool=tool,
        restart_of=restart_of, remote_precommand=remote_precommand, acknowledge=acknowledge,
    )
```

Also add one line to the wrapper docstring so the agent knows the arg exists:

```
    acknowledge: set True to proceed past a recall-gate hold after reviewing the
    surfaced pitfalls (resubmit with any parameter changes you decided on).
```

- [ ] **Step 5: Run tests to verify they pass (+ no regression)**

Run: `cd mcp-servers/compchem-tools && python -m pytest tests/test_recall_gate_wiring.py tests/test_run_shell.py -v`
Expected: PASS — new wiring tests plus the pre-existing run_shell suite (sanity that the shared module still imports/behaves).

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/compchem-tools/src/compchem_tools/tools/jobs.py \
        mcp-servers/compchem-tools/src/compchem_tools/server.py \
        mcp-servers/compchem-tools/tests/test_recall_gate_wiring.py
git commit -m "feat(compchem-tools): wire recall_gate + acknowledge into submit_job"
```

---

## Self-Review

**Spec coverage:**
- Unit 1 `select_warnings_for_tool` + `warnings_for_tool` → Task 1. ✓
- Unit 2 `recall_gate` (fail-open, hold-dict) → Task 2. ✓
- Unit 3 submit_job wiring + `acknowledge` (jobs.py + server.py) → Task 3. ✓
- Tool-scoped, promoted+staging, provisional flag, limit, ordering → Task 1 tests. ✓
- Hold-result contract (success:False, held:True, reason, pitfalls, instruction) → Tasks 2 & 3. ✓
- Acknowledge bypass; no-hits proceed; tool=None proceed; fail-open on exception → Task 2 tests. ✓
- Held submission never launches; acknowledged launches; gate-None launches → Task 3 tests. ✓
- All-schedulers (gate runs before the scheduler branch) → Task 3 (gate placed before scheduler dispatch). ✓
- Out-of-scope (auto-apply, structured rules, surfaced_in_session, write-side) → not in plan. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step is complete. ✓

**Type consistency:** `recall_gate(tool, command, project_dir, acknowledge)` identical in Task 2 definition, Task 3 call site/monkeypatch, and tests. `warnings_for_tool(tool, project_dir, limit)` identical in Task 1 definition and the gate's `_warnings_for_tool` indirection. Result-dict keys (`title`/`type`/`provisional`/`summary`/`source`) consistent across Task 1 producer, Task 2 consumer, and all assertions. Hold-dict keys (`success`/`held`/`reason`/`tool`/`pitfalls`/`instruction`) consistent across Tasks 2-3 and tests. ✓
