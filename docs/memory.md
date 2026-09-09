# The notebook

Magnolia's distinguishing property is not that it runs software but that it keeps a written account
of having run it. This document describes what is recorded, how an observation becomes a standing
rule, what happens unattended, and where the mechanism is known to be imperfect.

## What is recorded, and where

Every project carries a notebook at `projects/<name>/.magnolia/`:

| Directory | Content |
|---|---|
| `sessions/` | The journal of each session: tool calls, commands, outcomes |
| `runs/` | One record per computation — configuration, status, results |
| `staging/` | Draft notes, observed once, not yet committed to |
| `entries/` | Project notes that have held up across sessions |
| `queue/` | Events awaiting ingestion, chiefly results returning from a cluster |
| `session-notes/`, `archive/`, `backups/` | Working files, superseded entries, safety copies |

Because the notebook lives inside the project directory, it travels with the data: move the project
to another machine and the account moves with it. It is deliberately **not** tracked by the
repository — it belongs to your science, not to the assistant.

Two capture paths feed it, and they differ in what they can see:

- **The tool journal** records what was invoked and how it ended. It is exact, and it is blind to
  reasoning: it knows that a docking run completed with a given score, not what you concluded from
  the score.
- **Conversation distillation** submits the transcript to the memory model and asks it to extract
  the scientific findings. This is the path that captures interpretation — *"the contact map places
  F2 within 4 Å of R272"* — and it is the more valuable of the two, and the less reliable. See
  [§ The distillation ceiling](#the-distillation-ceiling).

You may write to the notebook directly at any point, and it is worth doing:

> *"Note that this docking failed because the peptide was too flexible."*

> *"Record that DOI 10.1021/… recommends AIR restraints for this class of target."*

Such annotations are permanent entries alongside the automatic ones, and — being stated rather than
inferred — they are never lost to a distillation failure.

## From a note to a rule

Not everything noticed deserves to be remembered for ever. Observations therefore rise through three
levels, each demanding more evidence than the last. The first two are the assistant's own notebook;
the third is a document in the repository, which is the point of the exercise — the last step is
where a machine observation becomes something a person has signed.

| Level | Location | Content |
|---|---|---|
| 1. Draft note | `.magnolia/staging/` | Noticed in a single session; not yet committed to |
| 2. Project note | `.magnolia/entries/` | A finding about *this* project, corroborated more than once |
| 3. Rule | `opencode_cc_mem/rules/` | Durable enough to apply on *every* project; read at every session start |

**Level 1 to 2 is automatic.** A draft becomes a project note once the same observation has arisen
in **at least two distinct sessions** and Magnolia is confident in it. A single enthusiastic session
is not enough, which is exactly what the two-session requirement exists to prevent.

**Level 2 to 3 is proposed, not performed.** Once a project note has held up across **three**
sessions, Magnolia nominates it. Before the nomination reaches you it is examined by three
independent review passes, any of which may veto it on scientific grounds and at least two of which
must approve; it is also compared against the rules already in force, so that a candidate
contradicting an existing rule is flagged rather than quietly filed.

What is then drafted is a **rule, not a copy of the note**. A project note carries the shape of the
observation that produced it — how often it was seen, the symptom, the remedy. A rule states the
resulting instruction and points back to the note as its evidence. Elevation is therefore an
editorial act, which is the second reason a person is asked to sign it.

**Nothing is written until you agree.** The proposal waits in
`.magnolia/reflex/promotion-proposal.json`, and is raised again at the start of every session until
it is dealt with. To see what is pending:

> *"Are there any proposed rules waiting for review?"*

Magnolia writes a readable summary to `magnolia-review/promotions.md` (`memory_review_promotions`):
each candidate, the sessions it came from, what the review passes said, and the drafted text. You
say which to accept and which to reject (`memory_apply_promotions`), and only the accepted ones are
written into `rules/` as ordinary Markdown. Rejected proposals are dismissed for good. The source
note is archived rather than deleted, so the evidence behind a rule outlives the note.

Because `rules/` is under version control, an accepted rule arrives as a reviewable diff and can be
reverted like any other commit — which is also the mechanism by which promoted rules are versioned,
the notebook's own versioning covering `entries/` and `staging/`.

**Why a person is in the loop.** Magnolia is good at noticing that something has become a habit. It
is in no position to judge whether the habit is good science. The system therefore nominates and a
person decides: a deliberate boundary, not an unfinished feature.

You need not wait to be asked. If you already know that something should be doctrine, say so — and
say *which* of the three homes you mean, since they are read at different moments and the wrong
choice is either ignored or expensive:

| Home | Read | Suited to |
|---|---|---|
| `rules/` | Every session | Standing procedure, never-skip gates. Every line is paid for each time; keep it short |
| `.opencode/skills/<name>/SKILL.md` | When the task matches the description | Tool-specific protocols, of any length: they cost nothing until that tool comes up |
| `.magnolia/` | Retrieved per task | Learnings — written by Magnolia, reviewed by you, not filed by hand |

The dividing line is provenance rather than length: doctrine and protocols are authored and reviewed
through git; learnings are observed and proposed.

## Unattended consolidation

`magnolia-selfreflex` performs the same housekeeping while you are elsewhere: it compacts old
session logs, distils recent sessions into staged entries, consolidates stale or duplicated project
notes, and ingests events queued from the cluster. It is intended to run from cron:

```cron
0 11 * * * /path/to/opencode_cc_mem/softwares/bin/magnolia-selfreflex /path/to/projects/my_project
```

This keeps the notebook tidy, and — more to the point — ensures that a lesson won in a long session
is not lost in it.

Results produced outside a session can be brought in by hand with the memory command-line interface:

```bash
magnolia-memory log-bash    …   # record a command and its outcome
magnolia-memory log-event   …   # record an event (a finished cluster job, say)
magnolia-memory sync-queue  …   # ingest queued events into the notebook
magnolia-memory init-vault --project-dir opencode_cc_mem/projects/my_project
```

## Browsing the notebook

`init-vault` scaffolds an [Obsidian](https://obsidian.md) vault configuration, after which
`projects/<name>/.magnolia/` opens as a vault: wikilinks between related entries, a graph view of
the project's knowledge, generated daily notes, and an `INDEX.md` grouping entries by kind — success
patterns, error resolutions, and so on. `generate-daily-note` produces a lab note for the day from
the recorded activity.

Nothing about this is required. The notebook is Markdown, and `grep` remains a perfectly respectable
interface to it.

## The distillation ceiling

This is the mechanism's principal known limitation, and it is stated here rather than buried.

Conclusions you reach in conversation are captured by submitting the transcript to the memory model.
The quality of that capture is bounded by the model's **effective** context window, which is
materially smaller than its advertised maximum: recall of material in the middle of a long input
degrades well before the hard limit is reached. In consequence:

- **Long sessions can have findings quietly skimmed**, even when the transcript nominally fits. The
  longer the conversation, the likelier a mid-transcript result is under-weighted.
- **Models with smaller windows are more exposed.** Large-context models handle ordinary sessions
  comfortably, but this is an assumption about the provider you chose, not a guarantee.
- **A hard overflow is not silently lost.** If the transcript exceeds the model's limit, or the call
  fails for another reason, distillation returns an error rather than "nothing found", and the
  session is left unmarked so that a later sweep retries it.

Chunked distillation — splitting a long transcript, distilling each piece within the effective
window, and merging — is planned and would remove the ceiling. Until then, the reliable remedy costs
one sentence: when you reach a conclusion that matters, say *"note that down: …"*. The finding is
then recorded immediately, by the direct path, and does not depend on the distiller at all.

## Related

- [architecture.md](architecture.md) — what is loaded at session start, and in what order.
- [`../WORKFLOW_GUIDE.md`](../WORKFLOW_GUIDE.md) — tips 8 and 9 cover codifying knowledge and
  reviewing proposals.
- [use-cases.md § 10](use-cases.md#10-returning-to-a-project-after-an-interruption) — the case the
  notebook exists for.
