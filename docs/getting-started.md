# Getting started

This document takes a fresh machine to a first working session. It should take about a quarter of
an hour, most of which is spent choosing a model provider.

## 1. Prerequisites

| Requirement | Note |
|---|---|
| Python 3.11 or newer | `python3 --version`. Available by default on most recent Linux and macOS systems |
| A POSIX system | Linux or macOS. Windows is usable through WSL but is not exercised by the maintainers |
| [OpenCode](https://opencode.ai) | The terminal client through which one converses with Magnolia |
| A model provider | See § 3 |

The scientific software itself (HADDOCK3, GROMACS, ORCA and the rest) is **not** a prerequisite.
Magnolia installs tools into `opencode_cc_mem/softwares/` as they are needed, and a session that
only reads results or plans an experiment requires none of them.

## 2. Installation

```bash
git clone https://github.com/HolobiomicsLab/Magnolia-AI.git
cd Magnolia-AI

python3 -m venv .venv
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-tools
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-memory
```

Both packages go into the **same** environment. compchem-tools imports compchem-memory internally,
so installing the first without the second yields a server that starts and then fails on its first
call.

If `pip` is missing — some minimal Python builds omit it — run `.venv/bin/python3 -m ensurepip
--upgrade` before the two installation commands.

**Verify:**

```bash
.venv/bin/python3 -c "import compchem_tools.server, compchem_memory.server; print('Helper programs are ready.')"
```

This imports the two server modules, so a broken installation is caught here rather than during the
first session. Do not proceed past a `ModuleNotFoundError`.

## 3. Choosing a model provider

Magnolia is provider-agnostic; OpenCode brokers the connection. Two model roles are configured
separately, and they need not come from the same provider:

- **Main model** — the agent you converse with. It reads the rules, chooses the tools, interprets
  results. This is where capability matters.
- **Memory model** — a background model that distils sessions, prepares handovers and re-ranks
  retrieved notes. It is called often and rewards a cheap, generous-context model.

Common arrangements, in no particular order:

| Plan | Provider | Note |
|---|---|---|
| Kimi Coding Plan | Moonshot AI | Economical; used daily by the author |
| GLM Coding Plan | Z.AI | Strong multilingual and coding performance |
| Claude Max | Anthropic | Higher usage limits than the Pro tier |
| ChatGPT Plus / Pro | OpenAI | Widely available |
| GitHub Copilot | GitHub | Uses an existing subscription |

Pay-as-you-go keys (Anthropic, OpenAI, Google Vertex AI, Moonshot, DeepSeek, OpenRouter) and local
models served through Ollama, llama.cpp or LM Studio are equally usable. The choice can be revised
later with `/connect` or `/models` inside a session.

One consideration bears on the memory model in particular: distillation quality is bounded by the
*effective* context window of the model, which is materially smaller than the advertised maximum.
See [memory.md § The distillation ceiling](memory.md#the-distillation-ceiling).

## 4. Configuring the models

```bash
./opencode_cc_mem/softwares/bin/magnolia setup my_project
```

The command proceeds in two steps: the main model is configured and verified, then the memory
model. Memory remains disabled until its model has answered a test call, so a mistyped model name
degrades the assistant rather than corrupting the notebook.

The second step may be deferred. Start a session and say *"set up memory"*; Magnolia follows
[`opencode_cc_mem/rules/memory-setup.md`](../opencode_cc_mem/rules/memory-setup.md) and configures
it in conversation. A restart is required afterwards, since the model choice is injected at launch.

Choices persist in `opencode_cc_mem/.magnolia/llm-setup.json` and are re-applied on every launch.
`magnolia memory status` reports what is in force; `magnolia memory set <model> [--provider P]`
changes the memory role alone.

## 5. The first project

```bash
./opencode_cc_mem/softwares/bin/magnolia my_project
```

Naming a project that does not exist prompts to scaffold it: the `.magnolia/` structure and a seed
`GOAL.md`. Then:

1. Place your inputs in `opencode_cc_mem/projects/my_project/raw_input/` — structures, sequences,
   whatever the campaign starts from.
2. Open with a message that states the objective, the location of the inputs, what has already been
   attempted, and how the result will be judged. [use-cases.md](use-cases.md) gives four worked
   openings; [`../WORKFLOW_GUIDE.md`](../WORKFLOW_GUIDE.md) gives a complete session.
3. Expect a proposal rather than an action. Magnolia is instructed to discuss before executing, and
   saying *"discuss the approach first, do not run anything yet"* reinforces it.

Two switches are worth knowing on day one:

- `magnolia --critic my_project` enables a flag-only claim critic. An independent judge model marks
  statements in Magnolia's reports that the invoked tools do not support. Verdicts land in
  `<project>/.magnolia/claim-critic/`. It annotates and changes nothing.
- `magnolia-run <command…>` wraps any shell command so that it is recorded in the session log —
  useful when you drive a tool yourself but want the notebook to know.

## 6. When something does not work

| Symptom | Likely cause and remedy |
|---|---|
| `ModuleNotFoundError` from the verification command | The installation did not complete. Repeat § 2; report the message if it persists |
| No scientific tools in the tool list | The compchem-tools daemon is not running. `opencode_cc_mem/softwares/bin/compchem-tools-daemon.sh status`, then `… start`; the log is `opencode_cc_mem/logs/compchem-tools-http.log` |
| No `memory_*` tools | Memory has not been configured, or its model failed verification. `magnolia memory status`, then § 4 |
| Magnolia acts before you have finished explaining | Say so: *"do not run anything yet, I am still giving you context"*. Models differ markedly in eagerness |
| A tool exists but the programme is missing | Ask Magnolia to install it; tools are placed under `softwares/` with a launcher in `softwares/bin/` |
| Notebook appears empty after a productive session | Distillation may have skimmed a long transcript. Ask explicitly: *"note that down: …"*. See [memory.md](memory.md) |

The daemon is started automatically by the `magnolia` launcher and is idempotent; starting it by
hand is only ever a diagnostic step.

## 7. Next

- [use-cases.md](use-cases.md) — what a campaign looks like end to end.
- [hpc.md](hpc.md) — registering a cluster, which is a configuration change rather than a code one.
- [memory.md](memory.md) — how notes become rules, and why you are asked to approve them.
