# Project Magnolia

**A quick-learning research assistant for computational chemistry**

Project Magnolia is like having a research assistant who reads fast, runs experiments, writes detailed lab notes — and never forgets what you taught them. You work with Magnolia by chatting in plain English, giving it tasks, correcting it when needed, and building up a shared body of knowledge over time.

---

## What is Magnolia, really?

Think of Magnolia as a **research assistant**, not a magic box.

- It can **install and run** scientific software (docking, design, simulation)
- It can **read your old results** and summarize what happened
- It can **design new experiments** based on what it learned
- It **takes notes automatically** in a project notebook so you don't have to remember every detail
- But like any research assistant, it works best when you give it **clear instructions**, point it to the right files, and **correct it** when it goes off track

The more you work with it, the better it gets — because it remembers your project's history.

---

## What can it do?

Magnolia can help with tasks like:

- **Molecular docking** — predict how a peptide or drug binds to a protein
- **De novo design** — generate new sequences that might bind better
- **Pocket prediction** — find the best spots on a protein for a drug to stick
- **Run tracking** — keep a lab notebook of which methods worked and which didn't

---

## How do I talk to it?

You chat with Magnolia using a program like **OpenCode**. Just type what you want in normal sentences, the same way you'd explain a task to a student in your lab:

> *"I want to dock this peptide onto my target protein and find the best binding pose. The files are in `projects/my_project/raw_input/`."*

> *"Design me 10 new peptides that might bind more tightly, but start with the motif that worked best before."*

> *"What did we learn from the last run? And did we ever try pocket 2?"*

Magnolia will figure out the steps, run the software, and report back. If it misunderstands, just correct it — like you would with a trainee.

---

## Key ideas (explained simply)

### 1. The Research Assistant
Magnolia is the AI agent — the part you chat with. It reads your requests, looks up what it knows, decides which tool to use, and reports back. It is fast and eager, but it needs you to point it to the right folders and tell it what matters.

### 2. The Lab Notebook (Memory System)
Every project has its own notebook at `projects/<name>/.magnolia/`. Magnolia writes down:
- Which experiments you ran
- Scores and results
- Errors you hit and how they were fixed
- What worked and what did not (for example: *"p2rank pocket restraints are better than manual restraints for this protein"*)

Because the notebook lives inside your project folder, you can move the project to another computer and the assistant still knows everything.

The notebook is also **browseable in Obsidian** — entries are stored as Markdown files with wikilinks, tags, and cross-references. You can open `.magnolia/` as an Obsidian vault to visually explore your project's knowledge base using the graph view, backlinks, and search.

You can also add your own annotations — just tell Magnolia:
> *"Note that this docking failed because the peptide was too flexible."*
> *"Record that paper DOI 10.1021/... suggests using AIR restraints for this target."*

These become permanent entries alongside the auto-generated ones.

Commands that run through `magnolia-run` are automatically logged to the session log, so even bash-driven scientific workflows are captured for later review.

#### From a note to a rule: the three levels

Not everything Magnolia notices deserves to be remembered forever, so notes move up through three levels and each step asks for more evidence than the last. The first two are the assistant's own notebook; the third is a document in this repository, which is the point — the last step is where a machine observation becomes something you have signed off on:

| Level | Where it lives | What it holds |
|---|---|---|
| **1. Draft notes** | `.magnolia/staging/` | Something noticed in a single session, not yet committed to |
| **2. Project notes** | `.magnolia/entries/` | Findings about *this* project that have held up more than once |
| **3. Rules** | `rules/` | Lessons durable enough to apply on *every* project — read at every session start |

**Level 1 → 2 happens on its own.** A draft becomes a project note once the same observation has come up in **at least two different sessions** and Magnolia is confident in it. One enthusiastic session is not enough — that is exactly what the two-session requirement is there to prevent.

**Level 2 → 3 is proposed by Magnolia and decided by you.** Once a project note has held up across **three different sessions**, Magnolia nominates it to become a rule. Before the nomination reaches you it is reviewed three times by independent passes of the model, any of which can veto it on scientific grounds, and at least two must approve. It is also compared against the rules you already have, so a candidate that contradicts one is flagged rather than quietly filed. Magnolia then drafts the rule text — and stops.

What it drafts is a *rule*, not a copy of the note. A project note carries the shape of the observation that produced it — how often it was seen, the symptom that prompted it, the fix. A rule states the resulting instruction and points back at the note as its evidence. Elevation is therefore an editorial act, which is the other reason a person is asked to sign it off.

**Nothing is written until you say so.** The proposal waits in `.magnolia/reflex/promotion-proposal.json`, and Magnolia raises it again at the start of every session until you deal with it. To see what is pending, just ask:

> *"Are there any proposed rules waiting for review?"*

Magnolia writes a readable summary to `magnolia-review/promotions.md` — the `memory_review_promotions` tool — listing each candidate, how many sessions it came from, what the review passes said, and the drafted rule. Tell it which to accept and which to reject (`memory_apply_promotions`) and only the accepted ones are written into `rules/`, as ordinary Markdown files you can edit or delete like any other. Because `rules/` is tracked by this repository, an accepted rule arrives as a reviewable change rather than an invisible one — you can read the diff, and `git revert` it if it turns out to be wrong. The source note is archived rather than deleted, so the evidence behind the rule survives it. Rejected proposals are dismissed for good and will not come back.

You do not have to wait to be asked. If you already know you want a rule, say so — see tip 8 in [`WORKFLOW_GUIDE.md`](WORKFLOW_GUIDE.md).

**Why you are in the loop.** Magnolia is good at noticing that something has become a habit. It is in no position to judge whether that habit is good science. So the system nominates and a person decides — a deliberate boundary, not an unfinished feature.

#### Self-reflex: automatic memory consolidation
Magnolia can also review its own notes while you are away. A small script called `magnolia-selfreflex` compacts old session logs, distills new learnings, and syncs any results sent back from HPC jobs. It is designed to run on a schedule — for example, every morning at 11 AM via cron:

```cron
0 11 * * * /path/to/softwares/bin/magnolia-selfreflex /path/to/projects/my_project
```

This keeps the notebook tidy and ensures that hard-won lessons do not get lost in long chat sessions.

#### Limitation: conversation distillation and your model's *effective* context window
Magnolia learns in two ways. The **tool log** captures what commands ran and how they ended — but it cannot see *reasoning*. So a conclusion you reach by interpreting results (for example, *"the contact map shows peptide F2 within 4 Å of Hsc70 R272"*) is only captured by the second path: **conversation distillation**, which feeds the whole chat transcript to your model and asks it to extract the scientific findings.

That second path is the more valuable one, and it has a quality ceiling set by your model's **effective** context window — *not* its advertised maximum. Models attend reliably over a span considerably smaller than the number they advertise; recall of facts buried in the middle of a long input degrades well before the hard limit ("lost in the middle"). Practical consequences:

- **Long sessions can have findings silently skimmed**, even when the transcript technically fits. The longer the conversation, the more likely a mid-transcript result is under-weighted or missed.
- **Smaller-window models are more exposed.** Large-context models (DeepSeek, Claude, GPT-4o-mini) handle typical sessions comfortably, but this is an assumption about *your* chosen provider, not a guarantee — pick a generous-context model if you run long sessions.
- **A hard overflow is not silently lost.** If the transcript exceeds the model's limit (or the call otherwise fails), distillation returns an error rather than "nothing found", and the session is left unmarked so it is **retried on a later sweep** instead of being discarded.

A planned improvement is to **chunk** long transcripts and distill each piece within the effective window, then merge — so recall stays high regardless of session length. Until then: for sessions where you reach important conclusions, the most reliable capture is to say so explicitly (*"note that down: …"*), which records the finding immediately and independently of the distiller.

### 3. The Tools
Scientific programs like **HADDOCK3**, **GROMACS** and **BoltzGen** live in a `softwares/` folder. Magnolia knows how to call them, but it installs them in isolated "fenced yards" so they don't interfere with each other. You don't need to memorize command lines.

Most of these have a dedicated connector of their own. A few — BoltzGen among them — are driven through the general command runner instead. From your side this makes no difference; it only means they do not appear as separate entries in the tool list.

---

## Folder layout (the simple version)

```
project_magnolia/
├── opencode_cc_mem/          # The "brain" and training of the assistant
│   ├── rules/                # Standing instructions, read at every session start —
│   │                         #   both hand-written and elevated from project notebooks
│   ├── .opencode/skills/     # Task protocols, loaded on demand when a task matches
│   ├── mcp-servers/          # Connectors for memory and tools
│   └── projects/             # Your actual science projects
│       └── my_project/   # Example project
│           ├── raw_input/    # Your PDB files and sequences
│           ├── runs/         # Results from experiments
│           └── .magnolia/    # The assistant's notebook for this project
├── softwares/                # Scientific software
│   ├── bin/                  # Shortcuts to run tools
│   ├── boltzgen/             # AI design tool
│   └── manifest.yaml         # List of installed tools
├── README.md                 # This file
└── WORKFLOW_GUIDE.md         # How to supervise the assistant effectively
```

**You mainly work inside `opencode_cc_mem/projects/<your_project_name>/`.** Everything else is managed by Magnolia.

---

## Getting started

### 1. Install OpenCode (Magnolia's chat interface)

Magnolia uses [OpenCode](https://github.com/anomalyco/opencode) as its chat interface — a free, open-source, terminal-based AI coding agent. Install it by following the instructions at [opencode.ai](https://opencode.ai). Think of OpenCode as the window you type into; Magnolia is the research assistant on the other side.

### 2. Pick a model provider

Magnolia needs an LLM provider to think with. It works with 75+ providers — here are the most common:

| Plan | Provider | Notes |
|------|----------|-------|
| **Kimi Coding Plan** (Moderato tier) | Moonshot AI | Budget-friendly. What I use daily. |
| **GLM Coding Plan** | Z.AI (Zhipu AI) | Strong multilingual and coding capabilities. |
| Claude Max | Anthropic | Higher usage limits than Pro tier. |
| ChatGPT Plus / Pro | OpenAI | Versatile, widely available. |
| GitHub Copilot | GitHub | Uses your existing Copilot subscription. |
| OpenCode Zen | OpenCode | Curated, tested models from the OpenCode team. |

Also supported via pay-as-you-go API keys: Anthropic, OpenAI, Google Vertex AI, Moonshot AI, DeepSeek, OpenRouter, and local models via Ollama / llama.cpp / LM Studio.

You'll be prompted to connect a provider the first time you launch (step 4). Your choice is saved, so this is a one-time thing. You can switch provider or model later by typing `/connect` or `/models` in the chat.

### 3. Build Magnolia's helper programs (one-time setup)

Magnolia's tools and memory system are powered by two small Python programs that run in the background. They need to be installed before your first session — this takes about 2 minutes and you only do it once.

**How they work.** These programs connect to Magnolia through a standard protocol (think USB, but for software). Magnolia routes your requests through them automatically:

- **compchem-tools** — runs scientific software (docking, simulation, HPC job management)
- **compchem-memory** — the lab notebook (remembers results, learns from sessions, tracks what worked)

Without them installed, Magnolia can chat but can't *do* anything or *remember* anything.

**You'll need:** Python 3.11 or newer installed on your computer. If you're not sure, open a terminal and run `python3 --version`. Most Linux and macOS computers already have this. If yours doesn't, install it from [python.org](https://www.python.org/downloads/) or your system package manager.

**Step-by-step:**

Open a terminal, `cd` into the `project_magnolia/` folder, and run these commands in order:

```bash
# 1. Create an isolated Python environment (so Magnolia's dependencies
#    don't interfere with anything else on your computer)
python3 -m venv .venv

# 2. Install the helper programs and all their dependencies.
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-tools
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-memory
```

**If `pip` is not found** (some minimal Python installs omit it), run this instead:
```bash
.venv/bin/python3 -m ensurepip --upgrade
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-tools
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-memory
```

**Verify it worked:** Run this command — if it prints the message below, you're all set:

```bash
.venv/bin/python3 -c "import compchem_tools.server, compchem_memory.server; print('Helper programs are ready.')"
```

You should see: `Helper programs are ready.`

If instead you see a `ModuleNotFoundError`, the install did not finish and Magnolia will not be able to run tools or take notes. Do not carry on to step 4 — re-run step 3, and if it still fails, report the error message as an issue. (This command deliberately loads the two programs themselves rather than just checking that their folders are on the path, so a broken install is caught here rather than in your first session.)

---

### 4. Start your first project

1. **Launch Magnolia with a project name** — open a terminal, `cd` into `project_magnolia/`, and run:
   ```
   ./opencode_cc_mem/softwares/bin/magnolia my_docking_project
   ```
   Replace `my_docking_project` with whatever you want to call your project. If the project doesn't exist yet, Magnolia will offer to scaffold it for you (creates the folder structure and a `GOAL.md`), then open the chat interface.

   The first time you launch, you'll be prompted to connect a model provider (see step 2 for options).
2. **Put your input files in the project folder** (for example, a protein PDB and a peptide PDB).
3. **Tell Magnolia what you want**, including where the files are and what you have already tried.

**New to working with AI assistants?** Read [`WORKFLOW_GUIDE.md`](WORKFLOW_GUIDE.md) for a step-by-step example of how to supervise Magnolia effectively — from planning an experiment to analyzing results.

Magnolia will:
- Check which tools are installed
- Suggest a plan
- Run the experiment
- Save results in `runs/`
- Write a note in the project notebook

---

## Working with high-performance computers (HPC)

Eventually you may want to run big calculations on a university cluster (using **Slurm**). Magnolia is designed for this:

- It can submit jobs to the cluster for you.
- The project notebook travels with your files, so when the cluster job finishes, Magnolia can read the results and continue.
- **Important:** Magnolia runs on your personal computer. The cluster just runs the heavy calculations.
- Use `magnolia-memory log-bash` and `magnolia-memory log-event` to log HPC job results, then `magnolia-memory sync-queue` to ingest them into the project notebook.

---

## Obsidian integration

You can open any project's `.magnolia/` directory as an **Obsidian vault** to browse the knowledge base visually.

```bash
# Scaffold Obsidian vault config for a project
magnolia-memory init-vault --project-dir opencode_cc_mem/projects/my_project

# Generate a daily lab note from today's memory data
magnolia-memory generate-daily-note --project-dir opencode_cc_mem/projects/my_project
```

Then open `projects/my_project/.magnolia/` as an Obsidian vault. You'll see:
- **Wikilinks** between related entries
- **Graph view** showing connections across your knowledge base
- **Daily notes** summarizing session activity, runs, and entries
- **INDEX.md** with entries grouped by type (success patterns, error resolutions, etc.)

---

## Quick tips

- **Be specific.** The assistant is smart, but it can't read your mind. Point it to files, mention prior runs, and say what you want changed.
- **Correct it.** If Magnolia is about to do something wrong, say *"Wait — do it this way instead."* Good collaboration means steering.
- **Check the notebook.** Ask *"What did we learn from the last run?"* or look in `projects/<name>/.magnolia/entries/`.
- **Let it install tools.** Magnolia will install software into `softwares/` automatically when needed.

---

## Need help?

If something goes wrong, just tell Magnolia:

> *"That didn't work. Can you check the error and try again?"*

Because of the memory system, it will often know exactly what went wrong and how to fix it.

---

## Licence

Magnolia is released under the **MIT License with a Non-Military Clause** — see [`LICENSE`](LICENSE). Clause 2 forbids use for the development, production or operation of weapons or military systems, or for any application that causes harm to civilian populations.

Because that clause restricts a field of use, this is **not** the unmodified MIT licence and is not an OSI-approved open-source licence. The source is available under the terms above; please do not describe the project as MIT-licensed or as open source.
