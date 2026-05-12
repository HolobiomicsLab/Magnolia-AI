# Magnolia Agent Behavior — Memory & Learning

This file is loaded by opencode at session start. It defines when to call memory
tools so the project actually learns from each session.

## Before any task

Call `memory_get_context(task_description=<your task in a sentence>)` as your
**first action**. The boot-context.md already loaded gives project-level memory;
this call gets task-specific reranked memory for what you're about to do.

## After resolving a tool error

Call `memory_record_learning` with `entry_type="error_resolution"`. Structure
the content as:

- **Symptoms:** what you observed (logs, error messages, behavior). Without
  this, future sessions cannot match the entry to their problem.
- **Cause:** what was actually wrong.
- **Fix:** the working solution, with code or commands when applicable.
- **Also:** red herrings you eliminated (things that turned out NOT to be the
  cause). These eliminate hypotheses and save the next attempt.

The session JSONL already records the `tool_error` and the subsequent
`tool_success`. This entry captures your *semantic understanding* of the fix —
context the decorator cannot observe.

## After a significant scientific result

Call `memory_record_learning` with `entry_type="success_pattern"` or
`"parameter_guidance"`. Required content:

- **Quantitative grounding:** specific scores, metrics, residue numbers,
  parameter values from the run. No vague summaries — say which run, which
  score, which sequence.
- **Exact parameters:** which sequences, structures, restraints, protocol
  produced the result.
- **CAVEAT (mandatory):** scope of applicability — which pocket, which binding
  mode, which protocol. Without this section, the entry will mislead future
  work that assumes the finding generalizes.

## After discovering an approach does NOT work

Call `memory_record_learning` with `entry_type="failure_pattern"`. Negative
findings save the next attempt — first-class learnings. Same structure as
success_pattern but record the failure mode and any red herrings.

## When prior memory is wrong

Update or correct it. Memory drifts as projects evolve; corrections are
themselves learnings. Note the prior incorrect claim and the corrected
information.

## Shell commands

Use `run_shell(cmd=...)` for any shell command. Opencode's bash tool is
disabled. `run_shell` invokes `magnolia-run`, which writes the session JSONL
and fires auto-assessment for recognized scientific tools.

## Periodically

Call `memory_confirm` to promote useful staging entries to the durable project
tier. The staging area is a low-pass filter; without confirmation, useful
learnings stay below the surface.
