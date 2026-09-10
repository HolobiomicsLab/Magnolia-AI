<div align="center">

# Project Magnolia

**A persistent-memory framework for computational research, developed through computational chemistry.**

<p>
  <img alt="Licence: MIT with Non-Military Clause" src="https://img.shields.io/badge/licence-MIT%20%2B%20Non--Military-4c6ef5?style=flat-square">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab?style=flat-square&logo=python&logoColor=white">
  <img alt="Protocol: MCP" src="https://img.shields.io/badge/protocol-MCP-6b4fbb?style=flat-square">
  <img alt="Interface: OpenCode" src="https://img.shields.io/badge/interface-OpenCode-333333?style=flat-square">
  <a href="https://holobiomicslab.cnrs.fr/"><img alt="Holobiomics Lab, CNRS" src="https://img.shields.io/badge/lab-holobiomicslab.cnrs.fr-4caf82?style=flat-square"></a>
</p>

</div>

---

## In brief

Magnolia helps a researcher retain and reuse the operational knowledge produced during
computational work: what was run, which choices were made, what failed, and what was learnt.
Its current tools and worked examples centre on computational chemistry, from docking and
molecular dynamics to quantum chemistry, on a workstation or a Slurm cluster. The memory,
review and provenance mechanisms provide a starting point for adaptation to other domains.
You work with it in ordinary prose, in a terminal.

The written record lives inside the project directory as Markdown, and relevant context is read
back in subsequent sessions. Git versions the shared rules and protocols; a separate local Git
repository tracks the notebook's staged and confirmed entries. Repeated observations can be
proposed to you for promotion into standing rules. A protocol refined on Friday can therefore
inform work resumed on Monday, together with the recorded reasons for the change.

Three properties distinguish Magnolia from a general-purpose coding agent:

- **Scientific tool integration.** The chemistry toolset includes typed wrappers for HADDOCK3,
  gnina, P2Rank, GROMACS, ORCA, Gaussian and xTB, with task-specific protocols. BoltzGen is
  driven through the recorded `run_shell` route.
- **A memory that is reviewed, not merely accumulated.** Learnings are staged, corroborated across
  sessions, and promoted into version-controlled rules only with your explicit agreement.
- **Provenance by construction.** Runs, commands, job identifiers and outcomes are recorded as they
  happen, in files you can read, diff and archive without Magnolia being present.

Magnolia is research software developed at the [Holobiomics Lab](https://holobiomicslab.cnrs.fr/)
(Université Côte d'Azur, CNRS). It is used daily on real projects; it is not a finished product,
and [§ Status and limitations](#status-and-limitations) says plainly where the edges are.

---

## Contents

1. [What Magnolia is, and what it is not](#what-magnolia-is-and-what-it-is-not)
2. [Scientific scope](#scientific-scope)
3. [Use cases and starting prompts](#use-cases-and-starting-prompts)
4. [How it is put together](#how-it-is-put-together)
5. [The notebook](#the-notebook)
6. [Installation](#installation)
7. [First session](#first-session)
8. [Running on a cluster](#running-on-a-cluster)
9. [Repository layout](#repository-layout)
10. [Status and limitations](#status-and-limitations)
11. [Documentation](#documentation) · [Contributing](#contributing) · [Citing](#citing) · [Licence](#licence)

---

## What Magnolia is, and what it is not

It is best understood as a **doctoral student who reads quickly, executes carefully, and takes
excellent notes** — and who, like any such student, requires direction, benefits from correction,
and should not be trusted with an unsupervised conclusion.

|  | Magnolia | Not Magnolia |
|---|---|---|
| **Register** | A collaborator you instruct in prose and correct mid-course | A button that turns a question into a result |
| **Scope** | Preparation, execution, monitoring and bookkeeping of computational experiments | Judgement about whether the science is sound — that remains yours |
| **Memory** | A project notebook you can read, edit and place under version control | An opaque model that has silently "learnt" your project |
| **Autonomy** | Proposes, then acts on your agreement; standing rules are promoted only by you | An autonomous agent left to run overnight without review |

The division of labour is deliberate. Magnolia is competent at noticing that something has become a
habit; it is in no position to judge whether that habit is good science. It therefore nominates, and
a person decides.

---

## Scientific scope

Computational chemistry is the current application domain and the source of the worked examples
below. Extending Magnolia to another field requires its own tool adapters or recorded shell
commands, written protocols, and verification criteria. The repository's general memory design
does not by itself establish scientific validity or tested support in another domain.

| Domain | Instrument | Exposed as |
|---|---|---|
| Binding-site prediction | P2Rank | `p2rank_predict` |
| Protein–protein and protein–peptide docking | HADDOCK3 | `haddock3_run`, `haddock3_parse_results`, `generate_restraints` |
| Small-molecule and covalent docking | gnina | `gnina_dock`, `gnina_parse_results`, `smarts_validate`, `alkyne_to_vinyl` |
| Molecular dynamics | GROMACS | `gromacs_setup`, `gromacs_run`, `gromacs_parse` |
| Quantum chemistry | ORCA, Gaussian, xTB | `orca_*`, `gaussian_*`, `xtb_optimize`, `xtb_singlepoint` |
| Generative binder design | BoltzGen | driven through `run_shell` (no dedicated wrapper) |
| Structure preparation | PDB tools, ACPYPE | `preprocess_pdb`, `validate_structure`, `run_acpype` |
| Cluster execution | Slurm over SSH | `submit_job`, `check_job`, `poll_jobs`, `fetch_job_results`, `cancel_job` |
| Literature | [Perspicacité](https://github.com/HolobiomicsLab/Perspicacite-AI) | optional MCP server on `localhost:8000` |

Each instrument carries a **skill** — a written protocol with its parameters, failure modes and
verification checklist — loaded only when the task calls for it. See
[`opencode_cc_mem/.opencode/skills/`](opencode_cc_mem/.opencode/skills/) for the full set, and
[`docs/tools.md`](docs/tools.md) for the complete tool reference.

---

## Use cases and starting prompts

Magnolia is instructed in prose, and the quality of the first message largely determines the quality
of the session. A serviceable opening states four things: **the objective**, **where the inputs
live**, **what has already been attempted**, and **the criterion by which the result will be
judged**. A domain-independent opening is:

> *"Review the scripts, logs and recorded findings from the last two runs in this project.
> Identify which protocol choices changed and why, retaining unsuccessful attempts and unresolved
> questions. Suggest what we should reuse when we resume on Monday; ask me to review any proposed
> standing rule before applying it."*

What follows are four worked application openings; a longer catalogue, with the follow-up
turns and the expected outputs, is in [`docs/use-cases.md`](docs/use-cases.md).

### 1. Structure-based design of a peptide binder

The canonical campaign. One begins from a target structure, asks where a peptide might plausibly
bind, docks a first series, reads the interaction pattern off the best poses, and lets that pattern
constrain a generative design round. Magnolia's contribution is not that it can call P2Rank and
HADDOCK3 — it is that the pocket chosen in step one, the restraints derived in step two and the
scores obtained in step three are all still legible three weeks later, when the fourth round has to
be justified to a co-author.

> *"I would like to identify a binder for the target in `raw_input/target.pdb`. Please begin by
> predicting the pockets with P2Rank and telling me which one you would favour and why, before
> running anything. I have no prior results for this system. I will judge the outcome on HADDOCK
> score and on whether the interface reproduces the contacts described for the natural ligand."*

### 2. Quantum-chemical characterisation of a small molecule

Molecular modelling in the strict sense: geometry optimisation, frequencies, frontier orbitals,
single-point energies at a better level of theory. The recurring difficulty is rarely the chemistry
but the bookkeeping — which conformer, which functional, which basis set, which revision of the
programme, and whether the frequency calculation was ever run on the optimised geometry rather than
on the input one. Magnolia proposes a method and basis appropriate to the question, submits the
work, parses the output, and records the level of theory alongside the numbers so that the methods
section can later be written from the notebook rather than from memory.

> *"For the ligand in `raw_input/ligand_A.mol2`, I need an optimised geometry and a frequency
> calculation to confirm it is a minimum, then a single-point energy at a higher level. Propose a
> method and basis set for a molecule of this size and justify the choice; use xTB first to
> pre-optimise. Record the level of theory in the notebook — I will need it for the methods
> section."*

### 3. A literature survey that ends up in the notebook

With [Perspicacité](https://github.com/HolobiomicsLab/Perspicacite-AI) running, bibliography ceases
to be a separate activity conducted in another window. Magnolia can assemble a knowledge base on a
question, read it, and — this is the part that matters — write the conclusion into the project
notebook as an annotated entry carrying its DOIs. The consequence is that a methodological choice
and the paper that motivated it are stored in the same place, so a decision taken in March can still
be defended in September.

> *"Before we set up the simulation, survey the recent literature on force-field choice for
> intrinsically disordered peptides — say the last five years. Build a knowledge base, summarise
> where the field agrees and where it does not, and record the conclusion as a note in the project
> notebook with the DOIs attached. Do not start any calculation yet."*

### 4. From notebook to manuscript

The last stage of a project is usually the one least well served by tooling. Because every run
carries its date, its configuration, its outcome and its annotations, Magnolia can reconstruct the
sequence of experiments as a narrative, distinguish what was actually observed from what was
inferred, and draft a methods section from the recorded parameters instead of from recollection.
Manuscripts kept on Overleaf are reachable through the git bridge, under the discipline set out in
[`opencode_cc_mem/rules/overleaf.md`](opencode_cc_mem/rules/overleaf.md).

> *"Summarise everything we have run on this project since June: the protocols, the parameters that
> changed between rounds, and the results. Separate clearly what was measured from what we
> concluded. Then draft a methods paragraph for each computational stage, using the parameters as
> recorded in the notebook, and flag anything you cannot substantiate from the record."*

**A note on register.** Correcting Magnolia mid-course is expected rather than exceptional:
*"Wait — store the outputs under `runs/` in dated subdirectories, not in a single folder."* Prompts
that instruct it to think before acting (*"discuss the approach first, do not run anything yet"*)
are worth their length, since a premature run costs queue time.
[`WORKFLOW_GUIDE.md`](WORKFLOW_GUIDE.md) develops this into a full worked session.

---

## How it is put together

Four components, communicating over the [Model Context Protocol](https://modelcontextprotocol.io).

| Layer | Component | Function |
|---|---|---|
| Interface | [OpenCode](https://opencode.ai) | The terminal client you converse with; loads rules, skills and the project's boot context at session start |
| Execution | **compchem-tools** | 36 typed tools for the scientific software and for Slurm; runs as a local HTTP daemon on `127.0.0.1:8001` |
| Memory | **compchem-memory** | 24 tools for the notebook: capture, retrieval, distillation, consolidation, promotion |
| Knowledge | `rules/`, `.opencode/skills/`, `.magnolia/` | Doctrine read every session, protocols loaded on demand, and learnings written by Magnolia itself |

Two design decisions are worth stating, since they explain otherwise puzzling details.

**compchem-tools runs as a daemon rather than as a client-spawned process.** When a tool call is
aborted, the client closes its side of the pipe; a locally spawned server would exit and would not
be respawned, and every scientific tool would silently disappear for the remainder of the session.
As an HTTP server it outlives such aborts, and the supervisor
([`compchem-tools-daemon.sh`](opencode_cc_mem/softwares/bin/compchem-tools-daemon.sh)) restarts it
if it ever dies. The `magnolia` launcher starts it for you.

**The two packages are one unit.** compchem-tools calls into compchem-memory internally; they must
be installed into the same environment. Installing compchem-tools alone is not a supported
configuration.

The knowledge base has **three homes**, distinguished by who writes them and when they are read:

| Home | Written by | Read |
|---|---|---|
| `rules/` and `AGENTS.md` | You, and promotion with your agreement | At the start of every session — keep it short, it is paid for each time |
| `.opencode/skills/<name>/SKILL.md` | You | Only when a task matches the skill's description |
| `<project>/.magnolia/` | Magnolia | Retrieved per task, re-ranked against what you are about to do |

Doctrine and protocols are authored and reviewed through git; learnings are observed and proposed.
The dividing line is provenance, not length.

---

## The notebook

Every project carries its own notebook at `projects/<name>/.magnolia/`: the runs performed, the
scores obtained, the errors met and how they were resolved, and the conclusions you dictated. Since
it lives inside the project directory, it travels with the data.

Observations rise through three levels, each demanding more evidence than the last:

| Level | Location | Content |
|---|---|---|
| Draft note | `.magnolia/staging/` | Noticed once, not yet committed to |
| Project note | `.magnolia/entries/` | Held up across **at least two** sessions |
| Rule | `rules/` | Held up across **three** sessions, reviewed, and accepted by you |

The first step is automatic; the second is not. A candidate rule is examined by three independent
review passes, of which at least two must approve, is checked against the rules already in force,
and is then drafted and left waiting in `.magnolia/reflex/promotion-proposal.json`. Nothing is
written until you say so. To see what is pending, ask:

> *"Are there any proposed rules waiting for review?"*

Because `rules/` is under version control, an accepted rule arrives as a reviewable diff and can be
reverted like any other change. The notebook is also browsable as an [Obsidian](https://obsidian.md)
vault, with wikilinks, graph view and generated daily notes.

`magnolia-selfreflex` performs the same consolidation unattended — compacting old session logs,
distilling recent sessions, and ingesting results returned from the cluster — and is intended to be
run from cron. The mechanism, together with its known ceiling (conversation distillation is bounded
by the *effective* context window of the model you chose, not by its advertised maximum), is
documented in [`docs/memory.md`](docs/memory.md).

---

## Installation

**Prerequisites.** Python 3.11 or newer, a POSIX system (Linux or macOS), and
[OpenCode](https://opencode.ai) as the chat client. A model provider is required; Magnolia is
agnostic and works with subscription plans (Moonshot Kimi, Z.AI GLM, Claude, ChatGPT, GitHub
Copilot) as well as pay-as-you-go keys and local models served through Ollama or llama.cpp.

```bash
git clone https://github.com/HolobiomicsLab/Magnolia-AI.git
cd Magnolia-AI

# One isolated environment for both helper packages
python3 -m venv .venv
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-tools
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-memory
```

Should `pip` be absent from a minimal Python installation, run
`.venv/bin/python3 -m ensurepip --upgrade` first.

**Verify the installation** before going further:

```bash
.venv/bin/python3 -c "import compchem_tools.server, compchem_memory.server; print('Helper programs are ready.')"
```

The check imports the two server modules rather than merely testing that their directories are on
the path, so a broken installation surfaces here rather than in your first session. A
`ModuleNotFoundError` means the installation did not complete: repeat the step, and if it persists,
open an issue with the message. Do not continue — without these packages Magnolia can converse, but
it can neither act nor remember.

---

## First session

```bash
# Step 1 — configure the models (run once)
./opencode_cc_mem/softwares/bin/magnolia setup my_project

# Step 2 — work
./opencode_cc_mem/softwares/bin/magnolia my_project
```

`magnolia setup` configures two model roles in sequence: the **main** model, which is the agent you
converse with, and the **memory** model, a cheaper background model that handles distillation,
handover and re-ranking. Memory stays disabled until it has been verified. The second step may be
deferred — start a session and say *"set up memory"*, and Magnolia will walk you through it.

Naming a project that does not yet exist offers to scaffold it: the `.magnolia/` structure and a
seed `GOAL.md`. Place your inputs in `projects/<name>/raw_input/`, and state your objective in the
terms of [§ Use cases](#use-cases-and-starting-prompts) above.

Two options are worth knowing early:

- `magnolia --critic <project>` enables a flag-only claim critic: an independent judge model marks
  statements in Magnolia's reports that are not supported by the tools actually invoked. It changes
  nothing and only annotates.
- `magnolia memory status` reports which models are in force.

Commands run through `magnolia-run <command…>` are recorded in the session log, so shell-driven work
is captured alongside tool calls.

---

## Running on a cluster

Magnolia runs on your own machine; the cluster performs the heavy computation. Jobs are submitted
over SSH, polled, and their results fetched back into the project, at which point the notebook
resumes where it left off.

Site facts live in
[`clusters.yaml`](opencode_cc_mem/mcp-servers/compchem-tools/src/compchem_tools/tools/clusters.yaml)
— an SSH alias, the scratch layout, a group account, whether the login node sits behind a tunnel.
Anything true of a person rather than of a machine (your username, your personal account) belongs in
`~/.config/magnolia/clusters.yaml`, which overrides the repository file key by key. **Adding a
cluster is a configuration change, not a code change**; the minimum is an SSH alias and a scratch
root, and every unset facility is simply skipped.

Two rules govern execution and are read at every session:
[`job_execution.md`](opencode_cc_mem/rules/job_execution.md) requires that every computation write
to `runs/YYYY-MM-DD_name/`, and [`prejob_check.md`](opencode_cc_mem/rules/prejob_check.md) requires
input verification before any submission — a job that fails ninety minutes in because residue
numbering disagrees between configuration and structure wastes an allocation that was not free.
See [`docs/hpc.md`](docs/hpc.md).

---

## Repository layout

```
Magnolia-AI/
├── opencode_cc_mem/                 # The assistant: instructions, connectors, projects
│   ├── AGENTS.md                    # Memory protocol, loaded at session start
│   ├── rules/                       # Doctrine, read every session (8 files)
│   ├── .opencode/skills/            # Task protocols, loaded on demand (10 skills)
│   ├── mcp-servers/
│   │   ├── compchem-tools/          # Scientific instruments and Slurm
│   │   └── compchem-memory/         # The notebook
│   ├── softwares/bin/               # Launchers: magnolia, magnolia-run, magnolia-selfreflex
│   └── projects/                    # Your projects (untracked)
│       └── my_project/
│           ├── raw_input/           # Structures and sequences
│           ├── runs/                # One dated directory per computation
│           └── .magnolia/           # The notebook for this project
├── docs/                            # Documentation
├── WORKFLOW_GUIDE.md                # How to supervise a session
└── README.md
```

You work almost entirely inside `opencode_cc_mem/projects/<name>/`. Research data are deliberately
untracked: the repository carries the assistant, not the science.

---

## Status and limitations

Magnolia is **research software under active development**, used daily on live projects at the
originating laboratory. Being candid about its edges is more useful than the reverse:

- **The promotion panel is unvalidated.** Candidate rules are reviewed by independent model passes,
  which is a safeguard, not a guarantee; the panel's agreement with expert judgement has not been
  measured. This is precisely why a person signs off.
- **Distillation has a quality ceiling.** Conclusions you reach in conversation are captured by
  distilling the transcript, and recall degrades well before a model's advertised context limit is
  reached. In long sessions, say *"note that down"* explicitly for anything that matters; the
  finding is then recorded immediately and independently. Chunked distillation is planned.
- **Verification is your responsibility.** The claim critic flags unsupported statements; it does not
  establish that a calculation was correct.
- **Coverage is uneven.** Instruments listed in [§ Scientific scope](#scientific-scope) have been
  exercised in real campaigns; BoltzGen in particular is driven through the general command runner
  rather than a dedicated wrapper.
- **Portability of per-machine configuration.** Cluster profiles, tool launchers and model choices
  are machine-local by design and are not carried by the repository.

The test suites are run per package, from each package directory:

```bash
cd opencode_cc_mem/mcp-servers/compchem-memory && ../../../.venv/bin/python3 -m pytest tests
cd opencode_cc_mem/mcp-servers/compchem-tools  && ../../../.venv/bin/python3 -m pytest tests
```

Both packages ship a `tests/conftest.py`, so collecting the two directories in a single invocation
fails on a module-name collision. Run them separately, as above.

---

## Documentation

| Document | Subject |
|---|---|
| [`WORKFLOW_GUIDE.md`](WORKFLOW_GUIDE.md) | Supervising a session: a worked example, common mistakes, prompting templates |
| [`docs/getting-started.md`](docs/getting-started.md) | Installation, model configuration, first project, troubleshooting |
| [`docs/use-cases.md`](docs/use-cases.md) | Extended catalogue of campaigns with starting prompts |
| [`docs/architecture.md`](docs/architecture.md) | Components, the MCP daemon, the session life-cycle |
| [`docs/memory.md`](docs/memory.md) | The notebook, promotion, self-reflex, and the distillation ceiling |
| [`docs/hpc.md`](docs/hpc.md) | Cluster profiles, Slurm conventions, the job life-cycle |
| [`docs/tools.md`](docs/tools.md) | Reference for all tools exposed over MCP |

---

## Contributing

Issues and pull requests are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) sets out the conventions:
Conventional Commits, per-package tests, and where a new rule, skill or cluster profile belongs.
Contributions that add an instrument are particularly welcome, and the shortest route is a skill
plus a thin typed wrapper rather than free-form shell.

## Citing

If Magnolia contributes to work you publish, please cite the repository. The metadata are in
[`CITATION.cff`](CITATION.cff), from which GitHub generates APA and BibTeX. Cite the commit or tag
you actually used: the assistant's behaviour depends on its rules, and those change.

## Licence

Magnolia is released under the **MIT Licence with a Non-Military Clause** — see
[`LICENSE`](LICENSE). Clause 2 forbids use for the development, production or operation of weapons
or military systems, and any application causing harm to civilian populations.

Because that clause restricts a field of use, this is **not** the unmodified MIT licence and is not
an OSI-approved open-source licence. The source is available under the terms above; please do not
describe the project as MIT-licensed or as open source. The project nonetheless follows open-science
practice — legible provenance, reviewable records, portable artefacts — and that distinction is
maintained deliberately.

## Acknowledgements

Developed by Tao Jiang at the [Holobiomics Lab](https://holobiomicslab.cnrs.fr/), Université Côte
d'Azur / CNRS. Magnolia relies on [OpenCode](https://opencode.ai) as its interface and on the
scientific software it drives — HADDOCK3, gnina, P2Rank, GROMACS, ORCA, Gaussian, xTB and BoltzGen —
each of which should be cited in its own right when used.

Related work from the same laboratory:
[Mimosa-AI](https://github.com/HolobiomicsLab/Mimosa-AI), a self-evolving multi-agent framework for
autonomous research; [Perspicacité](https://github.com/HolobiomicsLab/Perspicacite-AI), the
literature assistant Magnolia consults; and
[hpc-session](https://github.com/HolobiomicsLab/hpc-session), one authenticated window onto a Slurm
cluster.
