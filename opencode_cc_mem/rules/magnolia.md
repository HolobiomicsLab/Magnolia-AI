---
name: magnolia
version: 1.0
description: General agent behavior rules for Project Magnolia
---

# Magnolia Agent Behavior Rules

## Session Logging: Use `magnolia-run` by default

When running bash commands for scientific tools inside a Magnolia project, **prefer `magnolia-run`** so that the command, exit code, and working directory are automatically logged to `.magnolia/sessions/`.

**Good:**
```bash
magnolia-run boltzgen run design.yaml --output runs/test
magnolia-run haddock3 config.cfg
magnolia-run gmx mdrun -deffnm md
```

**Avoid (unless specifically asked otherwise):**
```bash
boltzgen run design.yaml --output runs/test
haddock3 config.cfg
gmx mdrun -deffnm md
```

Only skip `magnolia-run` for:
- Trivial file inspection (`ls`, `cat`, `head`)
- Git operations
- Editing or writing files
- When the user explicitly says not to log

## Memory Tracking

- After significant tool executions, consider calling `magnolia-memory log-bash` explicitly if `magnolia-run` was not used.
- Long-running jobs (>30 min) should be submitted via `submit_job` (local or Slurm) rather than run in the foreground.

## Project Structure

- Keep all inputs, runs, and memory inside `projects/<name>/`
- Use `softwares/bin/` wrappers for tool invocation
- Respect the `.gitignore` boundaries: do not track heavy environments or caches
