# Lazy rule loading via the skill tier — migration plan

**Status:** approved — decisions locked (no files moved yet; auto-retrieval is the prerequisite)
**Date:** 2026-06-23

**Decisions (2026-06-23):**
- HPC files (`slurm.md`, `hpc_azzurra.md`): **keep resident.** hsc70 (the active
  project) submits to Azzurra nearly every session, so lazy-loading them adds
  retrieval-dependency risk for ~zero benefit there; the savings would accrue
  only to non-HPC sessions. Trimming `hpc_azzurra.md` is worthwhile hygiene but
  is **decoupled** (trim in place, stays resident). De-bloating non-HPC sessions
  is a job for project-scoped loading (Option 3), **deferred**.
- Sequencing: **auto-retrieval trigger lands first** (prerequisite), then move.
- Provenance marker `source: authored_rule`: **yes** (cheap, clarifying).
**Goal:** stop loading all ~2,425 lines of `rules/*.md` into every session. Keep
behavior + safety rules always-on; move domain how-to rules to the skill tier,
where `assemble_context` surfaces them *only when relevant to the task*.

---

## Verified mechanism (why this works)

- opencode loads `rules/*.md` + `AGENTS.md` as **always-on `instructions`**
  (opencode.json). It does **not** treat them as skills.
- The **skill tier auto-surfaces**: `context_assembly.assemble_context` calls
  `select_relevant_skills` and reserves a **protected 30% of the token budget
  (~8k tokens)** for skills ([context_assembly.py:69]). So a skill loads
  automatically when the task is relevant — relevance-ranked, budget-capped.
- The matcher is the **heuristic keyword scorer** (no embeddings): it ranks on
  `name` / `description` / `tags`. `scan_skills_headers` **skips any file with no
  YAML frontmatter** → a frontmatter-less file is invisible to the skill tier.
- `get_skill(tool_name)` keys on frontmatter `name` (or filename substring) — so
  an explicit tool-keyed fetch path also exists.

**Corrected from earlier discussion:** the reflex machinery will **not** churn
authored rules. `consolidation.py` scans `staging/entries`, not `skills/`.
`promotion.py` only *reads* `skills/` via `_existing_rule_summaries` as the
consistency-check baseline (it never rewrites existing skill files) — which is
actually beneficial: promotion will check new proposals against the authored
domain rules. So an authored-vs-learned marker is **nice-to-have for provenance,
not required to prevent churn.**

---

## Stay vs move

**Stay in `rules/` (always-on — guaranteed present):**
| File | Why resident |
|---|---|
| `AGENTS.md` | memory behavior (when to call each memory tool) |
| `magnolia.md` | agent behavior (discussion-vs-execution, grounding) |
| `job_execution.md` | **safety** — output conventions, no-raw-`ssh sbatch` |
| `prejob_check.md` | **safety** — mandatory pre-submit checks |
| `slurm.md` | HPC submission — near-universal for HPC projects (kept resident) |
| `hpc_azzurra.md` | Azzurra cluster how-to — needed nearly every hsc70 session (kept resident; trim in place as separate hygiene) |

After moving the domain files out of `rules/`, the `rules/*.md` glob matches only
this resident set (+ `AGENTS.md` listed separately) — so **the move itself
reconfigures loading; no opencode.json edit needed.** (Verify the glob after moving.)

**Move to skill tier (`.magnolia/skills/`, relevance-loaded):**
| File | frontmatter `name` | tags present? | action |
|---|---|---|---|
| `haddock3.md` | haddock3 | ❌ **none** | **add tags** |
| `boltzgen.md` | boltzgen | ❌ **none** | **add tags** |
| `gromacs.md` | gromacs | ✓ | move as-is |
| `p2rank.md` | p2rank | ✓ | move as-is |
| `qm.md` | qm | ✓ | move as-is |
| `gnina_covalent.md` | gnina_covalent | ✓ | move as-is |
| `perspicacite.md` | perspicacite | ✓ | move as-is |
| `nature-skills.md` | nature-skills | ✓ | move as-is |

All already have frontmatter `name` (so all are matcher-visible). **Only
`haddock3.md` and `boltzgen.md` need tags added** — they'd otherwise match on
name/description only (weaker recall).

Suggested tags to add:
- `haddock3`: `[haddock3, docking, protein-peptide, restraints, caprieval]`
- `boltzgen`: `[boltzgen, generative, structure-generation]`

---

## The HPC files — RESOLVED: keep resident

`slurm.md` (243 lines) and `hpc_azzurra.md` (459 lines) **stay always-on.**
Rationale:
- **Frequency beats size.** hsc70 submits to Azzurra nearly every session, so
  these are near-always relevant. Lazy-loading them adds retrieval-dependency
  risk to the project that needs them most, while the context savings accrue only
  to non-HPC sessions.
- **Trim ≠ move.** Trimming `hpc_azzurra.md` (459 lines likely has redundancy) is
  good hygiene and is **decoupled** — trim it *in place*, keep it resident. A
  wrong "core vs reference" cut would risk pushing a safety-relevant line
  (VPN/partition/submission gotcha) into a tier that might not load.
- **Non-HPC de-bloat is Option 3's job, deferred.** Truly leaning out non-HPC
  sessions (paper/communication work) wants **project-scoped loading** (resident
  for HPC projects, skipped for others — wrapper templates instructions per
  `MAGNOLIA_PROJECT_DIR`). That's more infra work; backlog it. Don't approximate
  it with a risky trim-and-move.

Result: the resident set keeps the 2 HPC files; only the 8 true domain files move.

---

## The coupling dependency (important)

Moving rules to skills trades **"always present (bloated)"** for **"present only
if retrieved."** The skill surfaces via `assemble_context` / `memory_get_context`
— which rides on the agent actually **calling `memory_get_context` with a
descriptive task** (the always-on "first action" rule in AGENTS.md).

**So this migration must not ship before retrieval reliably fires.** If the agent
skips `memory_get_context`, domain guidance is now *absent* instead of merely
buried. Land (or land alongside) the **session-start auto-retrieval trigger**
first. Otherwise we trade bloat for absence.

---

## Provenance marker (optional)

Add `source: authored_rule` to each moved file's frontmatter so list/search can
distinguish authored domain docs from promoted learnings. Not required for
correctness (reflex won't churn them), but cheap and clarifying.

---

## Steps

1. **Land the session-start auto-retrieval trigger (separate spec) — prerequisite.**
   Do not move any rules before this.
2. Add `tags` to `haddock3.md` and `boltzgen.md`; add `source: authored_rule` to all 8 movers.
3. `git mv rules/{haddock3,gromacs,p2rank,qm,boltzgen,gnina_covalent,perspicacite,nature-skills}.md .magnolia/skills/`
   (HPC files stay in `rules/`.)
4. Verify: `rules/*.md` now globs only the resident set (incl. slurm + hpc_azzurra);
   a docking task surfaces the haddock3 skill via `memory_get_context`; an xTB task does not.
5. Update any rule cross-references (e.g. magnolia.md points to
   `rules/job_execution.md` — fine; check no resident file points to a moved one).
6. *(Separate hygiene, not blocking):* trim `hpc_azzurra.md` in place.
7. *(Backlog):* project-scoped rule loading (Option 3) for non-HPC sessions.

## Verification
- Resident set after move = `AGENTS.md`, `magnolia.md`, `job_execution.md`,
  `prejob_check.md` (+ HPC if option 1).
- `memory_get_context(task="dock peptide X against Hsc70 with HADDOCK3")` returns
  the haddock3 skill in its skill slot; the same call for an xTB task does not.
- Session token footprint drops by ~the moved line count.

## Rollback
`git mv` the files back to `rules/`. Pure relocation + frontmatter additions;
no code changes, fully reversible.

## Decisions — all resolved (2026-06-23)
1. HPC files: **keep resident** (Option 1). Trim `hpc_azzurra.md` in place as
   separate hygiene; project-scoped loading (Option 3) backlogged.
2. `source: authored_rule` marker: **yes**, on all 8 movers.
3. Auto-retrieval trigger lands **first** (prerequisite) — its own spec.

## Next artifact
The prerequisite: a spec for the **session-start auto-retrieval trigger** (a
plugin/boot hook that runs `memory_get_context` with a task-derived query on the
first message), since the move can't safely land until that exists.
