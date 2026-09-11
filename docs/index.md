# Documentation

Project Magnolia is a persistent-memory framework for computational research, with tools and
worked examples currently centred on computational chemistry. This directory holds the reference
documentation; [`../README.md`](../README.md) is the shorter introduction, and
[`../WORKFLOW_GUIDE.md`](../WORKFLOW_GUIDE.md) is the practical guide to supervising a session.

## Reading order

For a first installation, read in this order:

1. **[Getting started](getting-started.md)** — platform prerequisites, installation, separate
   model credentials, offline capture, first-session/restart checks and troubleshooting.
2. **[Use cases](use-cases.md)** — two worked continuity examples (a failed docking run and a
   metabolomics preprocessing comparison), followed by the chemistry/research prompt catalogue.
3. **[Supervising a session](../WORKFLOW_GUIDE.md)** — a worked conversation, the mistakes that
   cost the most time, and prompting templates.

For a deeper account of how the system behaves:

4. **[Architecture](architecture.md)** — the framework and harness, APIs and MCP, the session
   life-cycle, and how knowledge is stored and versioned.
5. **[Memory](memory.md)** — the notebook, the promotion of a note into a rule, unattended
   consolidation, and the known limits of conversation distillation.
6. **[Cluster execution](hpc.md)** — cluster profiles, Slurm conventions, the job life-cycle, and
   how results return to the notebook.
7. **[Tool reference](tools.md)** — every tool exposed over MCP, grouped by domain.
8. **[Harness adaptation](harness-adaptation.md)** — service setup, the manual memory loop,
   OpenCode hook diagnostics, transcript limits and checks for an alternate client.
9. **[Domain adaptation](domain-adaptation.md)** — a first domain pilot, protocol template,
   wrapper/assessor changes and evidence needed before routine use.

## Conventions used throughout

| Notation | Meaning |
|---|---|
| `projects/<name>/` | Shorthand for `opencode_cc_mem/projects/<name>/`, the root of one project |
| `.magnolia/` | The project notebook; excluded from the main repository, with a local Git history for `entries/` and `staging/` |
| `runs/YYYY-MM-DD_name/` | The mandatory output convention for any computation |
| *"…"* in italics | A prompt addressed to Magnolia, to be typed as prose |

Paths in these documents are given relative to the repository root unless stated otherwise.

## Where to report a problem

Open an issue on the repository. A useful report states what was asked, what Magnolia did, and what
the relevant files contain — the session log under `.magnolia/sessions/` and the run directory are
usually the two pieces of evidence that settle the question.
