# Daemon agents plan — the self-evolve loop (2026-10-07)

Supersedes/extends `docs/agents-daemon-design.md` (2026-09-24, mailbox watcher +
doorbell + FINAL ANSWER gate) and the 09-25 self-evolve staging note (the loop
map with two missing connectors). This version fills those connectors with
things built since.

## The loop the user wants

1. **literature** keeps the paper KBs current: periodic sweeps surface new
   papers, digests them, and scores importance (T1–T4).
2. If a paper is interesting for Magnolia/xiulian, literature drops an
   **idea ticket** into xiulian's inbox.
3. The **xiulian daemon agent** picks it up, prototypes the idea on a
   throwaway branch, and runs it through the **replay-eval** acceptance gate.
4. The gate report lands back; **a human decides merge** — never the loop.
5. Generalized: a **general-purpose daemon** that activates whichever project
   agent a letter addresses — literature is one lane, xiulian is another,
   research projects get the same rail.

## What already exists (state 2026-10-07)

| Piece | State |
|---|---|
| Daemon (`softwares/bin/magnolia-agents-daemon`, 343 lines) | built; mailbox watcher, `.task.md` letters, sequential queue, FINAL ANSWER gate + auto-resume, state/log under `.magnolia-agents/`; single-instance flock; `MAGNOLIA_AGENTS=0` kill switch |
| Doorbell (`@literature`/`@xiulian` in chat → letter) | built; dual-shape since 2026-10-07 |
| Inbox protocol (`rules/inbox.md`) | standardized; replies go to the sender's mailbox; `*.task.md` = auto-runnable, free-form = read-and-answer |
| literature-digest skill (digest + T1–T4 importance) | built, in `.opencode/skills/` |
| Perspicacité sweeps (paper discovery) | working (agentic sweeps, month_sweep) |
| **Replay-eval acceptance gate** | **BUILT since the 09-25 plan**: frozen corpus (`hsc70_bakeoff`, 646 slices), arms, blind judge (pinned GLM regime), five pre-registered gates; v6-repair arm running today |
| Label store + judge certification | built 2026-10-06/07 (judge-vs-human: 29/30 suppression agreement) |
| Idea-ticket format | **still missing** (was stage 3) |
| Literature → xiulian ticket flow | **still missing** (was stage 3's delivery half) |
| Xiulian prototype-on-branch daemon behavior | manual today |
| Scheduled literature sweeps (the "keeps KB up to date" trigger) | the sweep tooling exists; the SCHEDULED trigger does not (today it runs on demand) |

## The agents

### A. literature (papers in → digests + idea tickets out)

- **Trigger A1 — scheduled sweep:** the daemon gains a periodic task type
  (cron-style in the watcher loop: run the perspicacite agentic sweep over the
  watched topics every N days). Output goes to the literature KB as today.
- **Trigger A2 — digest:** new papers get digested via the literature-digest
  skill (evidence with character offsets, relevance + implementation value for
  Magnolia, T1–T4 importance).
- **Trigger A3 — the interesting threshold:** T1/T2 digests (or a digest whose
  "implementation value" section is non-empty) produce an **idea ticket** in
  `projects/xiulian/inbox/from-literature/`.

**Rails:** literature writes only inside its own project + the two inboxes;
no code changes anywhere; sweeps are event-scheduled, not LLM-looping (idle
cost ≈ zero). Nothing auto-runs unless it is a `.task.md` (existing daemon
rule).

### B. xiulian builder (idea ticket in → prototype + gate report out)

**There is no xiulian-specific runner.** The builder is just a headless
xiulian session activated by the general daemon (C), using xiulian's own
rules, skills, and memory — the same machinery an interactive xiulian session
uses. A bespoke daemon-side runner would duplicate the agent with worse
tools. (Design change 2026-10-07, user-suggested simplification.)

What the lane does with a ticket, by its own instructions (rules/skills —
human-readable, no daemon code):
- creates a throwaway branch `exp/idea-<slug>` from `experimental`, prototypes
  there, runs the **unit suite** and — when the idea touches the memory
  pipeline — a **replay-eval arm** against the frozen corpus;
- writes the reply letter with the gate report (arm name, the five metrics vs
  gates, links to the run dir) and flips the ticket's status;
- **merge is always the human's decision** (replay-eval README rule: never
  auto-merge; the gate report informs the merge discussion, no more).

One genuinely daemon-side piece: a replay arm takes ~1.5 h, longer than the
daemon's 30-min task timeout. The lane answers "arm started, report pending"
and the ticket closes at `prototyped`; when the job lands, the compchem-tools
job-notify poller already writes the notice, and the daemon re-delivers the
ticket with the result attached (a `wait_for: job:<run_id>` field in the
ticket header) so the lane evaluates the gate and writes the final reply.
That watcher is generic — any lane can use `wait_for`.

**Rails:** prototypes only on `exp/idea-*` branches; work only inside its own
project + inboxes; the FINAL ANSWER gate + auto-resume stay; 30-min timeout
for the interactive part; one task at a time.

### C. The general-purpose activator (THE daemon — the only daemon)

The daemon's mailbox watch is already per-agent-generic in structure; the
generalization is a **registry** instead of hardcoded names, and the registry
is the whole design:

- `projects/<name>/agent.yaml` declares a project agent: `{name, mailbox,
  workdir, model, allowed_paths, max_runtime}`. A project with an agent.yaml
  is a daemon lane; a project without one never gets tasks. **xiulian is just
  a lane** — literature is just a lane; research projects are future lanes.
- The doorbell stops hardcoding `[literature, xiulian]` and reads the registry
  (new agents appear without code edits).
- Unknown agent → the letter goes to `from-unknown` (the known bug item gets
  fixed by the registry).
- Long-work lifecycles are generic: `wait_for: job:<run_id>` re-delivers the
  ticket when the poller's notice lands.

## The missing connector: the idea-ticket format

Extends `rules/inbox.md` (`*.task.md` + status header) with a ticket schema:

```markdown
---
status: open            # open | prototyped | evaluated | accepted | rejected
from: literature
to: xiulian
date: YYYY-MM-DD
ticket: idea
importance: T2          # digest tier
paper: "arXiv:2609.27334"
gate: replay-eval       # which acceptance gate applies (or "unit")
---

## Idea
One paragraph: what to try, why it matters, which Magnolia subsystem.

## Evidence
Pointers into the digest (offsets) or the paper.

## Prototype sketch
The smallest change that would test the idea; which corpus/arm it should
replay against.

## Acceptance
What the gate report must show for a merge discussion to happen.
```

`status` lifecycle: open → prototyped (branch exists) → evaluated (gate report
attached) → accepted/rejected by the human. Only `open` tickets auto-run.

## Failure modes we already know (fold into the build)

- Helpers end turns with narration, not answers → FINAL ANSWER gate +
  auto-resume (already in the daemon).
- Headless permission wall → helpers answer on stdout; the daemon writes files
  (already folded in).
- Bare `opencode run` global default model is stale → always pass `--model`
  (already folded in; default deepseek-flash, override `MAGNOLIA_AGENTS_MODEL`).
- Shared-config pin race (helper sessions inherit the last project's MCP env)
  → the per-tree daemon port work (2026-10-05/06) plus per-agent project
  render fix this: each agent lane gets its own rendered config + port.
- Empty DeepSeek thinking output → the answer gate turns it into a visible
  error.

## Build order (each step independently verifiable)

1. **Idea-ticket schema** — extend rules/inbox.md with the ticket block above;
   a `ticket.example.md` under xiulian's inbox. (Docs only; no code.)
2. **Agent registry** — `agent.yaml` schema + the daemon reads it; doorbell
   reads the registry instead of the hardcoded pair. Fixes `from-unknown`.
   With this the daemon IS the general activator — every lane, including
   xiulian, is just a registry entry.
3. **Ticket workflow content** — xiulian's rules/skills gain the idea-ticket
   workflow (branch, prototype, gate, reply letter shape). This is prose +
   the `wait_for` field; the only code is the daemon's `wait_for` re-delivery
   (a ~30-line watcher over the existing job-notices file).
4. **Scheduled literature sweep** — a `schedule` section in agent.yaml
   (`every: 3d`) + the daemon's timer. Cheap once the registry exists.
5. **End-to-end smoke** — one hand-written idea ticket through the whole
   loop, watched once by the human; then it runs unsupervised.

Everything past step 1 is reversible via kill switches (`MAGNOLIA_AGENTS=0`
for the daemon; a per-agent `enabled: false` in agent.yaml).

## What never automates

- Merge decisions, gate-threshold changes, corpus changes, publishing.
- Literature's digest *content* decisions are automated; which tickets become
  prototypes is gated by importance tier AND the gate report, but the merge
  stays human.

## Relation to opencode v2

Nothing here blocks on the v2 migration, but two v2 items help: the in-process
capture (session_message) gives builder-lane sessions clean provenance, and
the per-tree/port isolation work already de-risks parallel lanes. The daemon
itself is version-agnostic (it shells out to `opencode run`); the CLI surface
it uses (`run --format json`, `--session` resume) survives v2 per the
migration assessment.
