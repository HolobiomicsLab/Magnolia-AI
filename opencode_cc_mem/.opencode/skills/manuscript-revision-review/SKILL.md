---
name: manuscript-revision-review
description: Item-by-item review of a collaborator's or editor's marked manuscript edits (LaTeX \revreplace/\revadd markup, tracked changes, marked PDF). Verifies each edit against the rendered marked build, analyses grammar and message fit with mandatory paragraph-level and cross-paper context, and records ACCEPT / KEEP / MODIFY / PARK verdicts in a decisions ledger. Use when walking through a colleague's review comments or revision markup on a manuscript with the author.
metadata:
  version: "1.0"
  last_verified: "2026-09-16"
  tags: "[manuscript, revision, review, latex, collaboration]"
---

# Manuscript revision review (item by item)

Walk a collaborator's marked edits with the author, one logical change at a
time, producing a durable record of verdicts and rationales. Worked example:
`projects/communication/paper/lfnothias_review/drafts/revisions/2026-09-15_tao/`.

## Before starting

1. **Identify the sources.** The markup copy (e.g. `\revreplace{old}{new}` /
   `\revadd{new}`) and, if it exists, the rendered marked PDF. The PDF is the
   visual ground truth.
2. **Extract the edits as inline markers.** Render old/new as
   `[-deleted-]` / `[+added+]` with unchanged words unmarked. Never derive
   spans by token-diffing the expanded texts alone: word-level markup leaves
   shared words *outside* the macros, so merged hunks leak unchanged words into
   both sides.
3. **Verify visually.** When a marked PDF exists, render pages (`pdftoppm`),
   find pages containing the revision colours, and read them. Confirm the
   extracted versions against what is actually struck/coloured. Check whole
   pages, not just sampled ones.
4. **Group logical changes.** One logical change may span several sentences;
   keep it as one item. Number the items in manuscript order.

## Working loop — one item per turn

**One decision per turn — never bundle.** Knock-ons or cross-paper findings
discovered mid-item go into the presentation as a "found, separate" flag and
are logged in the ledger's knock-on section for batch-application time. They
are NOT turned into second or third questions in the same turn, and they
never block the item's own verdict.

For each item, present in this shape (template evolved 2026-09-15,
lfnothias review; keep section order):

- **Header** — `**Item N — <section/¶, short title>** (hub file:line;
  Overleaf main.tex:line)`, plus counterpart locations when the issue
  spans copies.
- **Exact markup** — `…[-deleted-][+added+]…` with shared words unmarked.
- **Before / After** — `Mine: *…*` / `His: *…*`, bold on the changed words.
- **Analysis per part** — classify each sub-edit: sourced correction
  (accept), agenda detuning (judge against the argument), or register
  fix. For corrections, quote the citation-audit row / spec code
  (e.g. C-ED5) verbatim as grounding.
- **Grammar / lexis** — only when there is something to say.
- **Paragraph fit** — what must this sentence do in its paragraph?
- **Whole-paper fit** — antecedent links ("This gap…"), evidence anchors,
  register consistency.
- **Context (mandatory).** Other edits in the same sentence/paragraph
  (paragraph-level rewrite units) and same-issue edits elsewhere named as
  a pattern, so verdicts stay consistent; a pattern may be ruled on once.
- **Agenda lens, when the collaborator has a known editorial agenda**
  (e.g. generalization, standardization/provenance): state the policy
  explicitly — in the lfnothias review: accept architecture-level
  generalization and hedged adaptation claims; keep evidence anchors;
  reject unsupported cross-domain claims.
- **Cross-paper flags** — same-issue spots the edit does not cover:
  "found, separate — logged as a knock-on item", not part of the decision.
- **Options** — lettered A / B / C (with C1/C2 gradations for hybrids),
  exactly one marked recommended. For KEEP/MODIFY, draft the
  `% [KEPT]` / `% [HYBRID]` rationale inline so the author sees it before
  ruling.
- **My assessment:** one line.

Close with the plain-text prompt `Decision? (A / B / C / modify / park)`.
Ask verdicts in plain text, NOT via the question tool — the author replies
with a letter; the loop stays conversational.

On the answer: reply `Item N recorded (<verdict> — <one-line rationale>).`,
update the ledger immediately, then continue with the next item in the same
message: `Next: --- **Item N+1 …**`.

Verdict vocabulary: **ACCEPT** (adopt; give final wording if amended),
**KEEP** (reject; retain original), **MODIFY** (hybrid; exact final text),
**PARK** (revisit later). Option letters are per-item labels, not fixed
mappings.

## Batch mode — mechanical fixes

Collect purely mechanical edits into batches and rule on them in one go —
typos, grammar/agreement, hyphenation, and naming consistency where the
canonical form is already established. NEVER batch anything that touches
claims, citations, hedges, argument, or a wording choice the author might
weigh; those stay one-by-one.

- Present a batch once, as a compact list — per item: `hub file:line`
  (and Overleaf line) + exact markup, no per-item options or assessments.
- Single plain-text prompt: `Accept all? (or name exceptions)`. A named
  exception moves to the one-by-one track.
- Close with a one-line **My assessment:** before the prompt (e.g. "accept
  all" or "accept all except (a)"). Batching skips per-item options, not the
  agent's judgment.
- Record the batch as one ledger entry (`B1 — items 27–30, ACCEPT`) with a
  one-line trail per item inside the block, preserving the item-level record.
- Inherited micro-nits flagged during one-by-one items (outside the
  collaborator's markup) join a batch tagged `(author-side)`.
- Before presenting a batch, verify each item's exact markup and line numbers
  as for a normal item — batching changes the decision cadence, not the
  evidence standard.

## Record keeping

- Keep a review directory with `README.md` (protocol, verification basis) and
  `decisions.md` (the ledger).
- Update the ledger **as each decision lands**, never batched. Per item:
  verdict, final text, and for KEEP/MODIFY a bracketed `% [KEPT] …` /
  `% [HYBRID] …` rationale to attach at application time.
- At application: accepted wording goes into the canonical manuscript; a kept
  original keeps its text plus the rationale comment at the spot.

## Pitfalls

- Extracted spans that silently include unchanged words — verify visually.
- Calling a sourced correction a "softening" — check the audit row first.
- Deciding a sentence without its paragraph; patterns only emerge across items.
- Breaking a later reference's antecedent by softening the sentence it points
  back to.
- Batching the record — decisions live in the ledger, not in chat.
- Bundling a second decision into the same turn (knock-on handling, extra
  occurrences found by grep) — flag and log them; one `Decision?` prompt
  per item.
- Asking verdicts via the multiple-choice question tool instead of the
  plain-text `Decision? (A / B / C / modify / park)` prompt — breaks the
  conversational cadence the author expects.
- Presenting a compressed paraphrase of an edit instead of the template
  (exact markup + before/after + per-part analysis) — the author reviews
  wording, so show the wording.
