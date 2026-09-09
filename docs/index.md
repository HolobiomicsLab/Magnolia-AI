# Documentation

Project Magnolia is an agentic research assistant for computational chemistry. This directory holds
the reference documentation; [`../README.md`](../README.md) is the shorter introduction, and
[`../WORKFLOW_GUIDE.md`](../WORKFLOW_GUIDE.md) is the practical guide to supervising a session.

## Reading order

For a first installation, read in this order:

1. **[Getting started](getting-started.md)** — prerequisites, installation, model configuration,
   the first project, and what to do when something does not work.
2. **[Use cases](use-cases.md)** — how a campaign is actually conducted, with the opening prompts
   that set one up: docking and design, quantum chemistry, molecular dynamics, literature work,
   cluster campaigns, and writing up.
3. **[Supervising a session](../WORKFLOW_GUIDE.md)** — a worked conversation, the mistakes that
   cost the most time, and prompting templates.

For a deeper account of how the system behaves:

4. **[Architecture](architecture.md)** — the four components, the MCP daemon, the session
   life-cycle, and where knowledge is stored.
5. **[Memory](memory.md)** — the notebook, the promotion of a note into a rule, unattended
   consolidation, and the known limits of conversation distillation.
6. **[Cluster execution](hpc.md)** — cluster profiles, Slurm conventions, the job life-cycle, and
   how results return to the notebook.
7. **[Tool reference](tools.md)** — every tool exposed over MCP, grouped by domain.

## Conventions used throughout

| Notation | Meaning |
|---|---|
| `projects/<name>/` | Shorthand for `opencode_cc_mem/projects/<name>/`, the root of one project |
| `.magnolia/` | The notebook of the project that contains it; never tracked by git |
| `runs/YYYY-MM-DD_name/` | The mandatory output convention for any computation |
| *"…"* in italics | A prompt addressed to Magnolia, to be typed as prose |

Paths in these documents are given relative to the repository root unless stated otherwise.

## Where to report a problem

Open an issue on the repository. A useful report states what was asked, what Magnolia did, and what
the relevant files contain — the session log under `.magnolia/sessions/` and the run directory are
usually the two pieces of evidence that settle the question.
