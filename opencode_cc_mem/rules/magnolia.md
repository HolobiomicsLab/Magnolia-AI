---
name: magnolia
version: 2.0
description: General agent behavior rules for Project Magnolia
---

# Magnolia Agent Behavior Rules

## Shell commands

Opencode's bash tool is disabled. **Use `run_shell` for every shell command**;
it invokes `magnolia-run`, which logs the command, exit code, and working
directory to `.magnolia/sessions/` and fires auto-assessment for recognized
scientific tools.

**Examples:**

```python
run_shell(cmd="haddock3 config.cfg")
run_shell(cmd="gmx mdrun -deffnm md")
run_shell(cmd="ls runs/")
```

## Memory

Refer to `AGENTS.md` for when to call each memory tool. The key points:

- Start every task with `memory_get_context`.
- After a fix, call `memory_record_learning(entry_type="error_resolution", ...)`.
- After a noteworthy result, call `memory_record_learning(entry_type="success_pattern", ...)` with a mandatory CAVEAT.
- After a confirmed failure mode, call `memory_record_learning(entry_type="failure_pattern", ...)`.

## Long-running jobs

Long-running jobs (>30 min) should be submitted via `submit_job` (local or
Slurm) rather than run in the foreground.

## Project structure

- Keep all inputs, runs, and memory inside `projects/<name>/`.
- Use `softwares/bin/` wrappers for tool invocation.
- Respect the `.gitignore` boundaries: do not track heavy environments or caches.
