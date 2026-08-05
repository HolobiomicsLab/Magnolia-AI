---
name: nature-skills
source: authored_rule
description: How to use the nature-skills repo (https://github.com/Yuan1z0825/nature-skills) for Magnolia communication tasks — manuscript drafting, prose polishing, figure generation, and paper-to-presentation conversion.
version: 1.0
last_verified: 2026-06-13
tags: [communication, writing, figure, presentation, nature]
---

# Nature-Skills Integration

The [nature-skills](https://github.com/Yuan1z0825/nature-skills) repo provides
Nature-style academic writing, figure, and presentation workflows. Magnolia uses
it as an on-demand reference library for communication tasks.

## Setup (once)

```bash
git clone https://github.com/Yuan1z0825/nature-skills.git ~/ai-skills/nature-skills
```

The repo root is `~/ai-skills/nature-skills/`. All skills live under
`~/ai-skills/nature-skills/skills/`. Shared content is at
`skills/_shared/`.

## Task → skill mapping

| Communication task | Skill to load |
|---|---|
| Draft a manuscript section (abstract, intro, method, experiments, discussion, conclusion, title) | `nature-writing` |
| Polishing/editing prose to publication quality | `nature-polishing` |
| Creating publication-ready figures/plots from data | `nature-figure` |
| Converting a paper into a slide deck (.pptx) | `nature-paper2ppt` |
| Pre-submission mock reviewer assessment | `nature-reviewer` |
| Citation retrieval and reference management | `nature-citation` |
| Reviewer response letter | `nature-response` |

## General protocol for any skill

Every skill in nature-skills follows a **router + manifest pattern**:

1. Read `skills/nature-<topic>/SKILL.md` — the router
2. Read `skills/nature-<topic>/manifest.yaml` — maps axes to fragment files
3. Detect the axis values from the user's request
4. Load only the matching fragments from `static/`
5. Reach for `references/` files only when needed

**Always load from disk — never apply the skill logic from memory.**

---

## nature-writing — Manuscript drafting

Path: `~/ai-skills/nature-skills/skills/nature-writing/`

### Axes

| Axis | Values | Default |
|------|--------|---------|
| `paper_type` | research, methods, hypothesis, algorithmic, review | research |
| `section` | abstract, intro, related-work, method, experiments, discussion, conclusion, title | (ask user) |
| `language` | en, zh-to-en | en |
| `journal` | nature, nat-comms, generic | generic |

### Always-load (every drafting job)

```
skills/_shared/core/reader-workflow.md
skills/_shared/core/paper-type-taxonomy.md
skills/_shared/core/ethics.md
skills/_shared/core/terminology-ledger.md
skills/nature-writing/static/core/stance.md
skills/nature-writing/static/core/workflow.md
skills/nature-writing/static/core/output-format.md
```

### Fragment file map

Axis value → file (all under `skills/nature-writing/static/fragments/`):
- paper_type: `paper_type/{research,methods,hypothesis,algorithmic,review}.md`
- section: `section/{abstract,intro,related-work,method,experiments,discussion,conclusion,title}.md`
- language: `language/{en,zh-to-en}.md`
- journal: `journal/{nature,nat-comms,generic}.md`

### On-demand references (when needed)

| Trigger | File |
|---------|------|
| Section-level structure/argument order | `references/article-architecture.md` |
| Drafting/revising abstract | `references/abstract.md` |
| Introduction opening / Nature summary paragraph | `references/nature-summary-paragraph.md` |
| Introduction, task framing, contributions | `references/introduction.md` |
| Related work as topic synthesis | `references/related-work.md` |
| Method sections, module motivation | `references/method.md` |
| Experiments: baselines, ablations, metrics | `references/experiments.md` |
| Bounded conclusion | `references/conclusion.md` |
| Paragraph flow check (reverse outlining) | `references/paragraph-flow.md` |
| Manuscript self-review / rejection-risk audit | `references/paper-review.md` |
| Chinese-author repair patterns | `references/chinese-author-workflow.md` |
| Concrete examples | `references/examples/index.md` |

### Workflow summary (8 steps from `workflow.md`)

1. Surface missing claim/evidence/boundary before drafting
2. Write the one-sentence argument
3. Build the evidence chain
4. Select paper-type playbook → argument chain + drafting order
5. Apply section-specific structure rules
6. Draft with journal-specific constraints
7. Apply language-specific sentence/paragraph rules
8. Self-check: every claim grounded, boundaries stated, no invented content

---

## nature-polishing — Prose refinement

Path: `~/ai-skills/nature-skills/skills/nature-polishing/`

### Axes

Same as nature-writing:
- `paper_type`: research, methods, hypothesis, algorithmic, review (default: research)
- `section`: abstract, intro, results, discussion, conclusion, title, methods
- `language`: en, zh-to-en (default: en)
- `journal`: nature, nat-comms, generic (default: generic)

### Always-load

```
skills/_shared/core/reader-workflow.md
skills/_shared/core/paper-type-taxonomy.md
skills/_shared/core/ethics.md
skills/_shared/core/terminology-ledger.md
skills/nature-polishing/static/core/stance.md
skills/nature-polishing/static/core/failure-modes.md
skills/nature-polishing/static/core/output-format.md
```

### Key rules enforced

- Every sentence ≤ 30 words
- Section-aware tense: Results = past + quantitative; Discussion = hedging + mechanism
- Hedge calibration: demonstrate → suggest → may reflect
- Overclaim detection: flag absolutes, unwarranted causation, unverified "first"
- British English: signalling, colour, analyse, programme, modelling, behaviour
- LaTeX layout fixes: skip prose axes; load `references/latex-layout.md` directly

---

## nature-figure — Publication figures

Path: `~/ai-skills/nature-skills/skills/nature-figure/`

### BLOCKING GATE: Python or R?

**The backend axis is a blocking gate.** Before any figure work, ask "Python or R?"
and stop until the user answers. Do not default, guess, or generate code.

### Always-load

```
skills/nature-figure/static/core/contract.md
skills/nature-figure/static/core/stance.md
```

### Axis: backend

| Value | Fragment |
|-------|----------|
| python | `static/fragments/backend/python.md` |
| r | `static/fragments/backend/r.md` |

### Python rcParams (mandatory, always first)

```python
plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['font.sans-serif'] = ['Arial', 'DejaVu Sans', 'Liberation Sans']
plt.rcParams['svg.fonttype'] = 'none'  # keep text as <text> nodes
```

### Figure contract (from `contract.md`)

Before any code: write core conclusion → map evidence chain → classify archetype → set journal/export contract. Primary output is always `.svg`; `.png` at 300 dpi as secondary.

### Chart types (10 families)

Bar, line/trend, heatmap, scatter/bubble, radar/polar, distribution, forest/interval, area/stacked, image-plate, network/matrix.

### Bundled demos

`skills/nature-figure/assets/figures4papers/` — production scripts from published papers in *Nature Machine Intelligence* and ML/bioinformatics venues.

---

## nature-paper2ppt — Paper to slide deck

Path: `~/ai-skills/nature-skills/skills/nature-paper2ppt/`

### Axis: paper_type

| Value | Arc |
|-------|-----|
| discovery | question → evidence (default) |
| methods | problem → solution |
| resource | workflow → validation |
| clinical | design → inference |
| materials | property → mechanism / design → performance |
| review | evidence map |

### Always-load

```
skills/_shared/core/terminology-ledger.md
skills/nature-paper2ppt/static/core/principles.md
skills/nature-paper2ppt/static/core/toolchain.md
skills/nature-paper2ppt/static/core/workflow.md
skills/nature-paper2ppt/static/core/output-and-quality.md
```

### Key rules

- The paper's argument is the slide spine, not manuscript section order
- Chinese is default language for slides
- Select only figures that support the evidence chain; crop/split dense panels
- Use `python-pptx` to build actual `.pptx` (not an outline)
- Build a Terminology Ledger for consistent terms across slides
- 9-step workflow: intake → paper type → argument → slide plan → figures → content → build → QA → deliver

---

## Router-protocol-only skills

These are routed to in the mapping above but their axes/fragments are not yet
mirrored in this rule. Follow the **General protocol** (read `SKILL.md` +
`manifest.yaml` from disk, detect axes, load matching fragments) — documented
here by path only:

- **nature-reviewer** — pre-submission mock reviewer assessment.
  Path: `~/ai-skills/nature-skills/skills/nature-reviewer/`
- **nature-citation** — citation retrieval and reference management.
  Path: `~/ai-skills/nature-skills/skills/nature-citation/`
- **nature-response** — reviewer response letter.
  Path: `~/ai-skills/nature-skills/skills/nature-response/`

---

## When this rule is wrong

- The nature-skills repo changes its manifest format or fragment paths → re-probe and update
- A new skill is added that Magnolia needs → add to the task→skill mapping
- Magnolia moves to a different agent framework that supports native plugins → switch to plugin installation

## References

- Repo: https://github.com/Yuan1z0825/nature-skills
- License: MIT
- Local: `~/ai-skills/nature-skills/`
