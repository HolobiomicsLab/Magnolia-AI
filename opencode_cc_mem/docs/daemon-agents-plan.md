# Daemon agents plan — the self-evolving memory loop

Status: draft for review, 2026-10-07.
Supersedes the connector gaps identified in `docs/agents-daemon-design.md`
(2026-09-24) and the self-evolve staging note of 2026-09-25.

## 1. Objective

Implement the self-evolving memory loop:

1. A **literature agent** maintains the paper knowledge bases: it executes
   periodic discovery sweeps, digests new papers, and assigns each digest an
   importance tier (T1–T4 per the literature-digest skill).
2. Digests whose importance or implementation value exceeds a threshold
   generate an **idea ticket** in the xiulian project inbox.
3. A **xiulian builder lane** consumes idea tickets: it prototypes the
   proposal on a throwaway branch and evaluates it against the replay-eval
   acceptance gate.
4. The gate report is returned to the ticket; **merge decisions remain with
   the human operator**.
5. The daemon is generalized into a **registry-based activator**: any project
   that declares an `agent.yaml` becomes an executable lane.

## 2. Current state (2026-10-07)

| Component | Status |
|---|---|
| Daemon (`softwares/bin/magnolia-agents-daemon`) | Implemented. Mailbox watcher over `*.task.md` letters, sequential queue, FINAL ANSWER completion gate with auto-resume, state and logging under `.magnolia-agents/`, single-instance lock, kill switch `MAGNOLIA_AGENTS=0`. |
| Doorbell plugin (`@literature` / `@xiulian` mentions → letters) | Implemented; dual-shape (v1/v2) since 2026-10-07. |
| Inter-project inbox protocol (`rules/inbox.md`) | Standardized: replies target the sender's mailbox; `*.task.md` files are auto-executable, free-form letters are read-and-answer. |
| literature-digest skill (digest + T1–T4 importance tiers) | Implemented (`.opencode/skills/literature-digest/`). |
| Perspicacité discovery sweeps | Operational; currently invoked on demand only. |
| Replay-eval acceptance gate | Implemented: frozen corpus (`hsc70_bakeoff`, 646 slices), arm runner, blind judge (pinned GLM regime), five pre-registered gates. |
| Label store and judge certification | Implemented (2026-10-06/07); judge-vs-human suppression agreement 29/30. |
| Idea-ticket schema | Not implemented. |
| Literature → xiulian ticket delivery | Not implemented. |
| xiulian prototype-on-branch behavior | Currently manual. |
| Scheduled literature sweeps | Sweep tooling exists; no scheduler trigger. |

## 3. Agent specifications

### 3.1 literature agent

Responsibilities: (a) execute discovery sweeps on a schedule; (b) digest new
papers into the literature knowledge bases; (c) emit idea tickets for
high-importance results.

- **Sweep trigger:** a `schedule` section in `agent.yaml` (e.g.
  `every: 3d`) executed by the daemon's timer. The sweep invokes the existing
  Perspicacité agentic tooling; results are written to the literature KB.
- **Digest:** new papers are processed by the literature-digest skill
  (evidence extraction with character offsets, relevance and implementation
  value assessment, importance tiering).
- **Ticket emission rule:** digests at importance T1/T2, or any digest with a
  non-empty implementation-value section, generate an idea ticket in
  `projects/xiulian/inbox/from-literature/`.

Constraints: writes are restricted to the literature project directory and
the two inbox directories; no source-tree modifications; the sweep is
scheduled, not a continuous LLM loop (idle cost remains negligible). Tickets
are the only auto-executable artifact type; digests themselves are read-only
documents.

### 3.2 xiulian builder lane

There is no xiulian-specific daemon runner. The builder is a headless
xiulian session activated by the general daemon, operating with the same
rules, skills, and memory as an interactive xiulian session. A daemon-side
specialized runner would duplicate agent capability with inferior tooling
(design decision, 2026-10-07).

Ticket processing, governed by xiulian's own rules and skills:

1. Create a throwaway branch `exp/idea-<slug>` from `experimental`.
2. Implement the prototype described in the ticket.
3. Execute the unit test suite; when the ticket concerns the memory
   pipeline, execute a replay-eval arm against the frozen corpus.
4. Write the reply letter containing the gate report (arm identifier, the
   five metrics against their thresholds, run-directory links) and update
   the ticket status.

Long-running evaluations exceed the daemon's 30-minute task timeout. The
lane therefore replies "arm started, report pending" and closes the ticket
at status `prototyped`; upon job completion the compchem-tools job-notify
poller emits a notice, and the daemon re-delivers the ticket with the result
attached (`wait_for: job:<run_id>` in the ticket header) so the lane
evaluates the gate and emits the final reply. This re-delivery watcher is
generic and available to all lanes.

Constraints: prototypes only on `exp/idea-*` branches; writes restricted to
the xiulian project directory and inboxes; the FINAL ANSWER completion gate
and auto-resume remain active; 30-minute timeout for the interactive phase;
one task at a time.

### 3.3 General-purpose activator

The activator is the only daemon. Lane selection is registry-based:

- `projects/<name>/agent.yaml` declares a lane:
  `{name, mailbox, workdir, model, allowed_paths, max_runtime}`. A project
  with an `agent.yaml` is a lane; a project without one never receives
  tasks. xiulian and literature are registry entries, not special cases.
- The doorbell plugin resolves mention targets from the registry instead of
  the hardcoded `[literature, xiulian]` pair; new agents require no code
  changes.
- Letters addressed to unregistered agents are filed under `from-unknown`
  (resolves the known misrouting defect).
- Long-running work uses the generic `wait_for: job:<run_id>` re-delivery.

## 4. Idea-ticket schema

Extends `rules/inbox.md` (`*.task.md` with a status header):

```markdown
---
status: open            # open | prototyped | evaluated | accepted | rejected
from: literature
to: xiulian
date: YYYY-MM-DD
ticket: idea
importance: T2
paper: "arXiv:2609.27334"
gate: replay-eval       # acceptance gate identifier, or "unit"
---

## Idea
One paragraph: proposal, rationale, target Magnolia subsystem.

## Evidence
Pointers into the digest (with offsets) or the source paper.

## Prototype sketch
The minimal change that tests the idea; the corpus/arm to replay against.

## Acceptance
The gate-report outcome required for a merge discussion.
```

Status lifecycle: `open → prototyped → evaluated → accepted|rejected`. Only
`open` tickets are auto-executed. Merge decisions are excluded from
automation by design.

## 5. Known failure modes and existing mitigations

| Failure mode | Mitigation (already implemented) |
|---|---|
| Helper ends a turn with narration instead of an answer | FINAL ANSWER completion gate with automatic session resume (max 2 retries); unresolved runs are marked `error` with the narration preserved |
| Headless permission wall | Helpers emit answers on stdout; the daemon performs all file writes |
| Stale global default model | `--model` is passed explicitly on every run (default `deepseek/deepseek-flash`, override `MAGNOLIA_AGENTS_MODEL`) |
| Shared-config project pin race | Resolved by the per-tree daemon ports (2026-10-05/06) and per-lane config rendering; the opencode-v2 migration removes the class entirely |
| Empty DeepSeek thinking output | The completion gate surfaces it as a visible error |

## 6. Implementation plan

Each step is independently verifiable.

1. **Idea-ticket schema** — extend `rules/inbox.md` with the ticket block
   above; add a `ticket.example.md` under the xiulian inbox. Documentation
   only.
2. **Agent registry** — define the `agent.yaml` schema, implement registry
   reading in the daemon, and switch the doorbell to registry-based target
   resolution (also resolves the `from-unknown` misrouting). With this step
   the daemon is the general-purpose activator; xiulian is a registry entry.
3. **Ticket workflow content** — encode the idea-ticket workflow in
   xiulian's rules and skills (branch policy, gate execution, reply-letter
   shape). The only new daemon code is the generic `wait_for` re-delivery
   watcher over the existing job-notice file.
4. **Scheduled literature sweep** — a `schedule` section in `agent.yaml`
   consumed by the daemon timer.
5. **End-to-end verification** — one manually authored idea ticket through
   the complete loop, supervised once; unsupervised operation thereafter.

Steps 2–5 are reversible: `MAGNOLIA_AGENTS=0` disables the daemon, and a
per-lane `enabled: false` in `agent.yaml` disables an individual lane.

## 7. Excluded from automation

- Merge decisions, gate-threshold modifications, corpus modifications, and
  publishing remain human decisions.
- Digest content generation is automated; ticket prototyping is automated;
  the merge discussion is not.

## 8. Relation to the opencode v2 migration

The plan does not depend on the v2 migration. Two completed v2 items reduce
risk for parallel lanes: per-tree daemon ports (8011/8012) eliminate the
configuration pin race, and the in-process transcript capture
(`session_message` exporter) provides builder-lane sessions with complete
provenance. The daemon itself shells out to `opencode run`; its CLI surface
(`run --format json`, `--session` resume) is preserved in v2 per the
migration assessment.
