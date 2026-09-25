---
name: literature-digest
description: "Writing a literature digest note for one paper or report: fetch full text, extract evidence with character offsets, assess relevance and implementation value for our projects, and place the note in digests/<kb>/ (canonical) with an optional KB copy. Use when the user asks to digest, summarize, or assess a paper, arXiv ID, DOI, or report against Magnolia/Mimosa/xiulian. Do NOT use for broad field surveys across many papers (that is the perspicacite skill's agentic mode) or for pure retrieval without a written note."
metadata:
  version: "0.2"
  last_verified: "2026-09-25"
  tags: "[literature, digest, papers, evidence, schema]"
---

# Literature Digest — schema v0.2

A digest note is one file per paper. It records what the paper says (§1–§6),
what it means for us (§7–§8.5), and the evidence that backs every claim
(§9–§10). This schema is versioned and will change; bump `digest_schema` and
the skill's `metadata.version` together when it does. Validated on 17 notes
(2026-09-18 to 2026-09-24).

## When to use it

| Task | Use |
|---|---|
| User names a paper / arXiv ID / DOI and asks to "check", "digest", or "assess" it | this skill |
| Broad field survey, many papers, no single target | perspicacite skill (`agentic` / `literature_survey`) |
| One-off fact lookup, no note written | perspicacite search modes |

## File layout

- Canonical home: `projects/literature/digests/<kb_name>/<paper_id>.md`,
  grouped by the KB the paper belongs to. The KB copy (chunks) is derived;
  the file is the source of truth.
- Filename: the DOI with `/` → `_` (e.g. `10.48550_arXiv.2609.24972.md`).
  For reports with no DOI, use a slug (e.g.
  `anthropic_2026-09-23_art_rt.md`).
- Working artifacts (full text, screenshots) go in
  `projects/literature/runs/YYYY-MM-DD_<slug>/` per `rules/job_execution.md`.
  The digest's `source_run` points there.

## Workflow

1. **Resolve the identifier.** For an arXiv ID, fetch
   `https://arxiv.org/abs/<id>vN` first — title, authors, version date,
   license in one call. Do not construct the synthetic DOI until ingest.
2. **Get the full text into the run dir.** arXiv HTML version or
   `pdftotext` of a downloaded PDF. Record raw bytes and normalized length.
   Do NOT call `perspicacite get_paper_content` into your own context — a
   full paper is ~600 KB and overflows it. If a tool call returns the text
   to you, read the spilled tool-output file with a delegated subagent
   (Grep/Read with offset+limit), not directly.
3. **Whitespace-normalize** the full text (`re.sub(r'\s+', ' ', text)`),
   note the normalized length, and keep the normalized file — evidence
   offsets (§9) are character positions in it.
4. **Write the note** with the sections below. Every quantitative claim in
   §1–§6 must trace to an §9 evidence row; tag evidence quality
   (`[author-computed; single runs]` etc.) inline.
5. **Verify quotes with the standard recipe:** each §9 quote must be a
   contiguous substring of the normalized text
   (`normalized.find(quote) != -1`). Run this as a script, never by eye.
6. **Ingest into the matching bge KB** (embedding model must match the
   running server — see the perspicacite skill) and verify
   `added_chunks > 0`. `added_papers: 1, added_chunks: 0` is a silent
   failure: the paper never becomes searchable.
7. Record provenance in §10: version read, extraction method, KB ingest
   date + retrieval probe result, confidence, and what a panel pass should
   re-check.

## Frontmatter (YAML, required)

```yaml
---
digest_schema: v0.2
paper_id: "10.48550/arXiv.2609.24972"   # DOI, or slug for DOI-less reports
doi: "10.48550/arXiv.2609.24972"
title: "..."
authors: "First Last, ... (affiliation note)"
year: 2026
venue: "arXiv preprint arXiv:2609.24972v1 [cs.LG], 21 Sep 2026"
kb: self_evolving_agents                # which digests/<kb>/ dir it lives in
verdict: relevant                        # relevant | marginal | not_relevant
implementation_verdict: cherry-pick      # adopt | cherry-pick | watch | skip
targets: [magnolia, mimosa, xiulian]     # projects §7–§8 assess
assessor: "model name (project session, date)"
verified_by: "single-pass digest over one delegated full-text traversal; no independent panel"
confidence: medium                       # low | medium | high
status: triaged                          # triaged | digested | paneled
created: 2026-09-24
updated: 2026-09-24
source_run: runs/2026-09-24_rrsi_2609.24972/
supersedes: []
---
```

## Sections

Open the body with a one-line schema banner (like the existing notes): which
sections are science vs assessment, the action-ID convention, and which
normalized file the offsets point into.

- **§1 What it is** — one dense paragraph: what the paper does, headline
  numbers with evidence-quality tags, and the single biggest caveat.
- **§2 Problem & context** — the gap, prior work position, audience.
- **§3 Method** — mechanisms, formulas, setup, evaluation protocol. Concrete
  parameter values, not adjectives.
- **§4 Key results** — numbers with inline evidence-quality tags
  (`[author-computed; single runs]`).
- **§5 Benchmark & cherry-picking check** — strengths, risks, and a
  one-sentence takeaway: what to trust, what to treat as asserted.
- **§6 Limitations & open questions** — the authors' own plus ours.
- **§7 Relevance** — why it matters to each named target project,
  independent of any action. Point-for-point mappings to our code/protocols
  where they exist. End with a **Not relevant:** line naming what does not
  transfer.
- **§8 Implementation assessment** — verdict + an action table:
  `| ID | Target | Action | Cost | Status |`. Action IDs are stable and
  cross-paper: `ACT-<TARGET>-<SLUG>`, targets so far: `MEM` (Magnolia
  memory/harness), `MIM` (Mimosa), `BRIDGE`/`xiulian`, `CAMP` (science
  campaigns), `PERSP`. Close with **Not worth implementing** and a
  **Revisit trigger**.
- **§8.5 Comparative importance** — tier (T1–T4) + rank against all
  digested notes, basis = actionability × leverage × evidence strength; a
  rank table; short "vs X" bullets against the nearest neighbors; where it
  would and would not move the needle; confidence in the ranking. Adjacent
  ranks are within judgment noise — say so.
- **§9 Evidence** — table `| ID | Quote / data | Location | Offset |`,
  ~12–20 quotes, all verified by the standard recipe.
- **§10 Scope & provenance** — version read, extraction method +
  normalized length, quote-verification statement, KB ingest status, what a
  panel pass should re-check, identification notes (how the paper was
  resolved, lookalikes to not confuse it with).

## Conventions

- **Confidence.** A single-pass digest is `medium`. An adversarial-review
  panel pass raises it to `high` — record the panel in `verified_by` and
  §10. Panel first for notes that will drive design decisions.
- **Language.** Plain words, short sentences, one idea per sentence
  (`rules/magnolia.md` language rule). Numbers, offsets, and paths stay
  exact.
- **Citations inside the note.** Use the paper's own section/direction
  names; do not invent section numbers that are not printed in the paper
  (panel finding 2026-09-18).
- **Label inferences.** A claim we derived goes marked as inference; only
  verbatim or directly-computed claims are source facts.

## Common mistakes

| Mistake | Correct |
|---|---|
| Pulling full text into your own context | Extract to a run-dir file; delegate traversal to a subagent |
| Quoting from memory | Every §9 quote passes `normalized.find(quote)` in a script |
| Ingesting into a legacy (OpenAI-embedding) KB | Match the running server's embedding model (`*_bge`); check `added_chunks > 0` |
| Skipping the abs-page check | Fetch the abs page first — resolves version, title, and lookalike papers |
| Inventing section numbers for unnumbered headings | Cite the heading name |
| Ranking a new note without re-reading neighbors | §8.5 "vs X" bullets compare against the 2–4 nearest existing notes |
| Treating the digest file as a KB cache | File is canonical; KB chunks are derived and can be rebuilt |
