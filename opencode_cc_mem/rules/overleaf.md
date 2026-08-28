---
name: overleaf
description: Managing Overleaf-synced manuscripts via the git bridge — clone location, token storage, pull-before-push discipline, granular commits, and merge verification techniques.
version: 1.0
last_verified: 2026-08-26
tags: [overleaf, git, manuscript, sync, paper]
---

# Overleaf Manuscript Sync Rules

Overleaf has **no REST API for editing existing projects**. The sanctioned
programmatic path is the **git bridge** (premium feature): every project is a
git remote at `https://git.overleaf.com/<project-id>`.

## Path convention

- Each manuscript lives in a git clone at `projects/<name>/paper/overleaf/`.
- **One clone = one source of truth.** Never keep parallel downloaded copies
  (`main1.tex`-style snapshots) — that is what caused the Aug-2026 three-way
  divergence. Ad-hoc snapshots go to `paper/overleaf_backup_<date>/` and are
  retired once the clone exists.
- The whole `projects/` tree is machine-local (repo-root `.gitignore`), so the
  clone never conflicts with the kernel repo.

## Authentication

- Token from Overleaf Account Settings → Git integration (`olp_...`).
- Store once in `~/.git-credentials` (`https://git:<token>@git.overleaf.com`,
  mode 600) with `git config --global credential.helper store`. Never in the
  project tree or command lines.
- ⚠️ Tokens pasted into chat or commands end up in `.magnolia/sessions/`
  JSONL logs — rotate after setup if exposed.

## History semantics (why we use it)

- Each `git push` creates named version entries in Overleaf's History panel
  (commit message = label), attributed to the token owner.
- Browser edits do NOT become git commits automatically — only on
  pull/fetch or when a version is labeled. A pull therefore snapshots the
  collaborator's un-pulled work (possibly batched into one commit).
- No branches, no tags, no force-push; pushes can displace Overleaf
  track-changes/comments — don't mix active review comments with git pushes.

## Workflow (mandatory order)

1. `git pull` — snapshot collaborator's browser edits, avoid divergence.
2. Analyze differences **word-level** (python `difflib` on whitespace-normalized
   paragraphs), never by eyeballing wrapped-line diffs — hard line wrapping
   produces fake "changed" fragments and hides real sentence-level edits.
3. Apply changes as **small logical commits** (one section or one concern per
   commit) — this is the whole point: granular, labeled Overleaf history.
4. Verify before pushing:
   - assertion-guarded replacements (`assert s.count(old)==1`) so edits are
     atomic — a failed assert writes nothing;
   - re-extract the section and confirm it matches the intended source
     (whitespace-normalized compare);
   - `pdflatex` smoke-test; if errors appear, compile stashed HEAD to prove
     pre-existing vs newly introduced (`git stash` → compile → `git stash pop`).
5. `git push origin main`.

## Ownership split (communication project)

- Tao (git side): Introduction, Discussion, Methods wording, figures 1-2,
  abstract.
- Colleague (Overleaf browser side): case studies, stats block
  (placeholder numbers until pre-submission regeneration pass).
- Port only owned hunks; leave the other side's sections untouched.

## Style decisions (user-mandated, 2026-08-26)

- **No em-dash constructions (` --- `) anywhere in the manuscript.** Use
  colons (explanations), parentheses (asides), or commas (appositives).
  En-dashes for ranges (`700--900`) and `--` in pairs like
  `protein--peptide` are fine.
- Plain register: "requires" not "presupposes"; "custom scripts" not
  "bespoke scripts".

## Known state (2026-08-26)

- Clone: `projects/communication/paper/overleaf/` (project
  `6a32efabf95cb3e0d3ea905d`), local backup of pre-merge state in
  `paper/overleaf_backup_2026-08-26/`.
- 3 pre-existing compile errors in the colleague's new case-study text:
  `≤` (U+2264) at main.tex:173 needs `$\leq$`; two stray `\\` (no line to end).
- Stats placeholders: tracked-runs / entry counts are snapshots of a living
  system — regenerate from `.magnolia/runs/*.yaml` and
  `.magnolia/entries/*.md` counts right before submission.

## When this rule is wrong

- Overleaf changes git-bridge behavior (branch naming, auth) — re-verify
  against docs.overleaf.com.
- If Server Pro (self-hosted) is used instead of cloud, URLs and git-bridge
  enablement differ.
- If a future Overleaf public API appears, prefer it for file-level operations.
