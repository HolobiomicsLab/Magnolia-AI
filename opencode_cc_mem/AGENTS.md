# Magnolia Agent Behavior — Memory & Learning

This file is loaded by opencode at session start. It defines when to call memory
tools so the project actually learns from each session.

## First response: memory setup gate

Before anything else in your FIRST response of a session, check whether the
`compchem-memory` tools (e.g. `memory_get_context`) are present in your tool
list.

- **Present** → memory is configured; continue normally (call
  `memory_get_context` before any task, see below).
- **Absent** → memory is NOT configured yet. Do not wait to be asked:
  proactively tell the user, in 2-3 plain sentences, that (a) Magnolia has a
  memory system that distills sessions into reusable learnings, (b) it needs
  a cheap background model (default `deepseek-flash`) to run, and (c) you
  can set it up right now. Then follow `rules/memory-setup.md`. If the user
  declines, proceed without memory and do not nag again this session.

## Before any task

Call `memory_get_context(task_description=<your task in a sentence>)` as your
**first action**. The boot-context.md already loaded gives project-level memory;
this call gets task-specific reranked memory for what you're about to do.

**Recap exception:** if the request is a pure recap ("where were we?", "any
pending tasks?", "status?"), answer directly from the SESSION HANDOVER already
in boot-context — do NOT call `memory_get_context`. The handover is already in
context; the call re-fetches it at multi-second latency.

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

## When you change a job parameter from its default or prior value

Whenever you submit a run with a **non-default parameter, or one that differs
from a prior comparable run** — sampling level, ncores, walltime, memory,
restraint scheme, receptor/ligand choice — call
`memory_record_learning(entry_type="parameter_guidance")` **at the time you
choose the value**, not later. Required content:

- **Value chosen**, and the value it replaces (the default, or the prior run's
  value).
- **Why:** which prior result or reasoning motivated the change.

This is mandatory and is *not* covered by "after a significant scientific
result." Parameter rationales are **cross-run inferences** (e.g. "s10000 gave
n=4, so use s20000"), not error/success events — the auto-extractor cannot
capture them, and a staging `parameter_guidance` is never surfaced by
`memory_get_context`. If you don't write and promote it at decision time, a
future session will see the parameter with no rationale and may revert it to the
value that caused the original problem. (Promote critical parameter rationales
to the project tier via `memory_confirm`, or fold them into a rule, so they
surface.)

## After discovering an approach does NOT work

Call `memory_record_learning` with `entry_type="failure_pattern"`. Negative
findings save the next attempt — first-class learnings. Same structure as
success_pattern but record the failure mode and any red herrings.

## When prior memory is wrong

Update or correct it. Memory drifts as projects evolve; corrections are
themselves learnings. Note the prior incorrect claim and the corrected
information.

## When a tool returns project_switch_blocked

If any memory tool returns a JSON payload with `"status": "project_switch_blocked"`,
the user is trying to record work for a different project than this session is
pinned to. Do not retry. Relay the payload's `message` to the user plainly — they
need to start a new opencode session for the other project. One session works on
one project.

Reading another project's memory is fine and is not blocked — only writing is.

## Shell commands

Use `compchem-tools_run_shell(cmd=...)` for any shell command. Opencode's bash tool is
disabled. `compchem-tools_run_shell` invokes `magnolia-run`, which writes the session JSONL
and fires auto-assessment for recognized scientific tools.

## Periodically

Call `memory_confirm` to promote useful staging entries to the durable project
tier. The staging area is a low-pass filter; without confirmation, useful
learnings stay below the surface.

## Knowledge placement — three homes, one rule

Magnolia has exactly three knowledge homes. Choose by content class:

1. **`AGENTS.md` + `rules/*.md`** — always-on doctrine, injected in full every
   session: behavior discipline and never-skip gates only. Every line is paid
   every session — keep it small.
2. **`.opencode/skills/<name>/SKILL.md`** — task-shaped protocols (how to run
   haddock3, how to run an adversarial review, how to draft a manuscript).
   Loaded on demand when the task matches the description; bodies cost nothing
   until then. Shared skills are authored, git-tracked documents; the git
   review is the gate. Exception: your private cluster skill (`hpc-<cluster>`)
   lives in `~/.config/opencode/skills/` — outside the repository, so it can
   never be committed. Before doing a task that matches a skill's
   description, load it with the `skill` tool.
3. **`.magnolia/` memory tiers** — learned knowledge (session → staging →
   project entries). Surfaced by boot-context, `memory_get_context`, and the
   action-retrieval plugin.

Never put a protocol in the memory tiers or a learning in `.opencode/skills/`.

## Promotion is editorial, not a file copy

When a project-tier entry proves itself and should become durable doctrine or
protocol:

- **Do not** copy the entry verbatim into a skill/rules directory — a copied
  entry keeps its learning schema (id, observation counts, Symptoms-Cause-Fix
  body) and is alien among protocol docs.
- Instead: draft the distilled rule into the target skill or rules file (a
  pitfall row, a "Common mistakes" line, or a new section), with a pointer
  back to the source entry id. The source entry stays in its project tier as
  the evidence base.
- The `memory_review_promotions` / `memory_apply_promotions` flow implements
  this draft-and-review path: accepted drafts are written as standalone rule
  files into the git-tracked `rules/` directory and the source entry is
  archived (git-reversible). This is the only elevation path — the raw-copy
  `memory_promote` tool and the `.magnolia/skills` tier were retired
  2026-09-04. Every proposal carries a destination: a lesson that contains
  cluster-specific facts (your cluster's address, account name, VPN) is never
  written into shared files — accepting it marks it handled, and the review
  shows the text to copy into your private `hpc-<cluster>` skill. Destinations
  are defined in `magnolia-destinations.yaml` (next to `rules/`).

## Reviewing consolidation proposals

If `.magnolia/reflex/consolidation-proposal.json` exists with unapplied proposals
(check at the start of a session), surface them for the user:

1. Call `memory_review_consolidation` — it writes a readable review to
   `magnolia-review/proposals.md` and returns a summary.
2. Present the proposed merges briefly and ask the user to accept/reject/modify.
   The user may also edit `magnolia-review/proposals.md` directly.
3. Call `memory_apply_consolidation(accept=[accepted indices], reject=[rejected
   indices])`. Accepted merges are applied deterministically and committed
   (reversible via git); rejected ones are durably dismissed so they don't
   re-surface. The review directory is removed once every proposal has been
   handled (accepted or rejected).

Never apply a proposal the user did not confirm.

## Reviewing rule-elevation proposals

If `.magnolia/reflex/promotion-proposal.json` exists with unapplied proposals
(check at the start of a session), surface them for the user:

1. Call `memory_review_promotions` — it writes a readable review to
   `magnolia-review/promotions.md` and returns a summary.
2. Present each proposed elevation (entry, panel approvals, any ⚠ correctness or
   duplicate/conflict flags, the drafted rule) and ask the user to accept/reject.
3. Call `memory_apply_promotions(accept=[…], reject=[…])` (or `promote_raw=[…]`
   to elevate the entry verbatim instead of the draft). Applied rules are written
   and committed (reversible via git); the source project entry is archived. Edit
   the resulting rule file afterward if needed.

Never elevate a proposal the user did not confirm.
