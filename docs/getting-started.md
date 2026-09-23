# Getting started

Start with a small project that needs no scientific software. Verify installation
and capture, then check that one reviewed learning can be retrieved after a restart.
Once that works, add the instruments needed for your research.

The supplied launcher targets OpenCode. For a different client, install the Python
packages below and continue with [harness adaptation](harness-adaptation.md).
Commands run from the repository root unless stated otherwise.

## 1. Prepare the machine

| Requirement | Check | Purpose |
|---|---|---|
| Git and access to this repository | `git --version` | Source, protocols and local memory history |
| Python 3.11+, with venv and pip | `python3 --version` | Both Magnolia packages |
| Bash, GNU coreutils, curl and `setsid` | Checks below | Launching, execution logging and daemon supervision |
| OpenCode | `opencode --version` | Interactive harness and plugins |
| Main-model credentials | Authenticate in OpenCode | Conversation and tool use |
| Separate memory-model credentials | Step 4 | Extraction, handover and reranking |

Linux is the reference shell environment for these instructions. On Debian/Ubuntu,
the system packages are `git`, `python3-venv`, `python3-pip`, `curl`, `coreutils` and
`util-linux`; verify that the distribution's Python is at least 3.11. Windows users
need a Linux environment such as WSL for Magnolia's scripts. The Windows OpenCode
executable alone does not supply that environment.

Install OpenCode following its [official instructions](https://opencode.ai/docs/#install).
For example, with Node.js and npm already installed:

```bash
npm install -g opencode-ai
opencode --version
```

Check the shell utilities before launching:

```bash
command -v git python3 bash curl timeout setsid
date +%s%N
```

The date command must print digits only. A result ending in `N` is incompatible with
Magnolia's GNU nanosecond timestamps. `setsid` starts the tool-daemon supervisor;
`timeout` is used by the model setup probe.

**macOS:** the Python packages can be installed, but stock BSD `date` and the absence
of `setsid` prevent the supplied shell path from working as written. Use GNU coreutils
with its unprefixed commands on `PATH`; installing `gdate` alone is not enough. Without
`setsid`, run the tools server in a separate foreground terminal as described in
[harness adaptation](harness-adaptation.md#start-the-tool-server). This does not provide
automatic daemon restart. Check capture and reconnection before relying on this
arrangement; it is not an end-to-end macOS compatibility claim.

Scientific programs, a GPU, a cluster and Perspicacité are optional at this stage.
The Python packages expose interfaces; they do not install HADDOCK3, ORCA, GROMACS
or the other scientific executables. Each has its own dependencies and licence.

## 2. Install the Python packages

```bash
git clone https://github.com/HolobiomicsLab/Magnolia-AI.git
cd Magnolia-AI

python3 -m venv .venv
.venv/bin/python3 -m pip install --upgrade pip
.venv/bin/python3 -m pip install \
  -e opencode_cc_mem/mcp-servers/compchem-tools \
  -e opencode_cc_mem/mcp-servers/compchem-memory
```

The repository currently requires collaborator access. A clone error may concern
GitHub access or Git authentication, before Python is involved.

Install **both packages in the same environment**: execution imports memory. If pip
is absent, run `.venv/bin/python3 -m ensurepip --upgrade` and retry. Use the explicit
interpreter path to avoid installing into a different Python.

Verify imports against a disposable project. Importing the servers starts background
workers, so give them an empty directory rather than a research notebook:

```bash
MAGNOLIA_CHECK_DIR="$(mktemp -d)"
MAGNOLIA_PROJECT_DIR="$MAGNOLIA_CHECK_DIR" \
MAGNOLIA_RULES_DIR="$PWD/opencode_cc_mem/rules" \
  .venv/bin/python3 -c \
  "import compchem_tools.server, compchem_memory.server; print('Helper programs are ready.')"
.venv/bin/magnolia-memory --help
.venv/bin/python3 -m pip check
```

Expected: the readiness message, CLI subcommands, and no broken package requirements.
This checks Python imports, not OpenCode connectivity, model authentication or
scientific binaries. Resolve a `ModuleNotFoundError`, including one for `httpx`, before
continuing; both editable installs should use the current checkout.

## 3. Check capture without a model

This works without OpenCode or an API key. It records an explicitly executed command;
`log-bash` itself does **not** execute the command supplied to it.

```bash
MAGNOLIA_DEMO_DIR="$(mktemp -d)"
printf 'Magnolia capture check\n'
.venv/bin/magnolia-memory log-bash \
  --project-dir "$MAGNOLIA_DEMO_DIR" \
  --working-dir "$PWD" \
  --command "printf 'Magnolia capture check\\n'" \
  --exit 0 --result-summary 'Magnolia capture check'

.venv/bin/python3 - "$MAGNOLIA_DEMO_DIR" <<'PY'
import json
import pathlib
import sys
logs = list((pathlib.Path(sys.argv[1]) / '.magnolia' / 'sessions').glob('*.jsonl'))
events = [json.loads(line) for log in logs for line in log.read_text().splitlines()]
assert any(e['event_type'] == 'bash_execution' and e['exit_code'] == 0 for e in events)
print('Recorded command found in', logs[0])
PY
```

Expected: a JSONL file under `.magnolia/sessions/` containing the command and exit status.
This proves local logging only. Step 6 checks the additional interactive behaviour.

## 4. Configure the two model roles

| Role | Configuration | Credentials |
|---|---|---|
| Main agent | OpenCode provider/model ID | OpenCode authentication |
| Background memory | `magnolia setup` or `magnolia memory set` | Variables read by Magnolia's Python client |

Authenticate the main model with `opencode auth login`, then inspect IDs with
`opencode models` ([OpenCode CLI](https://opencode.ai/docs/cli/)). Choose an ID available
to your account; this guide does not prescribe a subscription.

The memory client currently implements four provider routes:

| Provider argument | Credential variable | Endpoint override |
|---|---|---|
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` |
| `anthropic` | `ANTHROPIC_API_KEY` | No dedicated override in this client |
| `openai` | `OPENAI_API_KEY` | `OPENAI_BASE_URL` |
| `kimi` | `KIMI_PLAN_MAGNOLIA_API_KEY` or `KIMI_API_KEY` | `KIMI_BASE_URL` |

Kimi uses its coding endpoint and Anthropic-style messages. Another OpenAI-compatible
service may work through `--provider openai` and `OPENAI_BASE_URL`, but must pass the
real probe. Compatibility with every local or hosted server is not established.
The routes are defined in
[`llm.py`](../opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/llm.py).

Supply the chosen credential through your local environment or secret manager before
launch. Keep keys out of the tracked template, prompts and notebook. OpenCode login
does not automatically configure the Python client, and background calls may have
separate billing. Setup performs real model calls.

```bash
./opencode_cc_mem/softwares/bin/magnolia setup onboarding_demo
```

The current launcher requires a project name even with `setup`. Accept the scaffold
prompt, but press Enter when asked for a goal. Scaffolding happens before model setup;
a nonempty goal can trigger an extra call using ambient credentials/defaults rather
than the model you are about to select. Fill the goal after configuration. Setup opens
OpenCode afterwards.

When deliberately testing a feature branch, apply the
[non-master override](#5-locate-and-reopen-the-first-project) to this setup invocation too.

Setup asks for the main model and verifies an explicitly entered ID, then probes the
memory model. Leaving the main field empty keeps OpenCode's default without testing
it. The current memory default is `deepseek-flash`; choose another model if appropriate.
A failed memory probe leaves memory disabled. You can converse in that state, but it
is not a working persistent-memory setup.

Inspect the saved selection in another terminal, from the repository root:

```bash
./opencode_cc_mem/softwares/bin/magnolia memory status
```

To change only the background model (replace this example ID with your provider's ID):

```bash
./opencode_cc_mem/softwares/bin/magnolia memory set deepseek-flash --provider deepseek
```

This writes the selection only after a successful call. Restart the client and any
separately managed processes after changing their model environment.

## 5. Locate and reopen the first project

If setup already opened `onboarding_demo`, keep that session for step 6. To return
to the project after quitting, launch without `setup`:

```bash
./opencode_cc_mem/softwares/bin/magnolia onboarding_demo
```

For a new project name, the launcher asks to scaffold it and creates
`opencode_cc_mem/projects/onboarding_demo/.magnolia/`, its subdirectories and `GOAL.md`,
renders the configuration, starts the tools daemon and opens OpenCode. For this demo,
edit `.magnolia/GOAL.md` to state “Verify that a reviewed learning survives a restart.”
For later projects, the scaffold's optional goal expansion uses ambient model settings;
leave it blank and edit the file if you need to choose the model first.

The launcher normally requires the code checkout to be on `master`. To deliberately
test a reviewed feature branch, use the existing override for that invocation:

```bash
MAGNOLIA_ALLOW_NONMASTER=1 ./opencode_cc_mem/softwares/bin/magnolia onboarding_demo
```

Do not switch or reset a working tree merely to silence the branch check. For a real
project, create `projects/<name>/raw_input/` for inputs and keep each calculation under
`runs/YYYY-MM-DD_description/` within that project. OpenCode starts in `opencode_cc_mem/`,
so give the agent the project path explicitly.

### Configuration that persists

| File | Purpose | How to change it |
|---|---|---|
| `opencode_cc_mem/.magnolia/llm-setup.json` | Local model selections and memory-enabled flag | Setup or `magnolia memory set`; ignored by Git |
| `opencode_cc_mem/opencode.json.template` | MCP connections, plugins, instruction paths and permissions | Review an intentional configuration change |
| `opencode_cc_mem/opencode.json` | Generated client configuration | Inspect; edits are overwritten on launch |
| `projects/<name>/.magnolia/GOAL.md` | Project purpose and criteria | Review and edit with the project |

The template enables optional Perspicacité at `localhost:8000/mcp`. If you do not run
it, set that server's `enabled` field to `false` in your template and review the diff.
Also review `permission.external_directory`: inherited paths are not a portable
access policy for your machine. Keep personal paths and keys out of shared config.
The launcher uses one generated config and active-project marker per checkout;
use one project session at a time.

## 6. Verify a complete first session

In another terminal, from the repository root:

```bash
./opencode_cc_mem/softwares/bin/compchem-tools-daemon.sh status
(cd opencode_cc_mem && opencode mcp list)
```

The first checks a supervisor; the second checks client connections
([OpenCode MCP docs](https://opencode.ai/docs/mcp-servers/)). Neither alone proves a
successful tool invocation. In the conversation, ask:

> In `projects/onboarding_demo`, retrieve context for this installation check. Use
> Magnolia's recorded shell tool to run `printf 'capture works\n'`, with its working
> directory set to that project. Show the exit status and the session file containing
> the event. Do not install scientific software or submit jobs.

Then exercise reviewed memory:

> Record a learning titled “Onboarding output convention”: this demo writes checked
> results under `runs/`, and this convention applies to this demo only. Show me the
> staging entry and its identifier before confirming it.

After checking it, request confirmation of that exact entry with `memory_confirm`.
Quit normally, reopen `magnolia onboarding_demo`, and ask:

> Retrieve the onboarding output convention, identify its source entry and explain
> its scope. Do not infer it from this message or create a new entry.

| Check | Evidence |
|---|---|
| Memory and execution connections | Successful `memory_get_context` and `run_shell` calls |
| Recorded execution | Command/result events in this project's `.magnolia/sessions/` |
| Explicit learning | Proposed Markdown under `.magnolia/staging/` |
| Confirmed learning | That entry under `.magnolia/entries/` |
| Continuity | New-session retrieval identifies the entry and its limited scope |

Tools may carry a client/server prefix. `memory_confirm` moves staging into project
memory; elevation into shared `rules/` is a separate review/apply operation. No rule
promotion is needed for this exercise. A missing event or memory is a failed check
even if the assistant produces a plausible answer.

## 7. Troubleshooting

| Symptom | Check and next action |
|---|---|
| Python import fails | Repeat both installs in the same `.venv`; record Python/package versions |
| `setsid` missing or timestamp arithmetic fails | Revisit step 1; a model change cannot repair shell dependencies |
| Main model works, memory does not | Check the separate key/model route and `magnolia memory status`; rerun its probe |
| Memory tools absent | Inspect generated `mcp.compchem-memory.enabled`; restart after successful setup |
| Execution tools absent | Check daemon/client status and `opencode_cc_mem/logs/compchem-tools-http.log` |
| Daemon says UP but calls fail | Status checks the supervisor, not an MCP round trip; inspect its log and port `8001` ownership |
| Shell wrapper missing | Service `PATH` needs this checkout's `.venv/bin` and `opencode_cc_mem/softwares/bin` |
| Records in the wrong notebook | Pass absolute `project_dir`; for shell, also set `cwd` to that project; restart for another project |
| `project_switch_blocked` | Start a new session for the intended project rather than retrying the write |
| Long command times out | `run_shell` defaults to 90 s and caps foreground work at 110 s; use `background=true` or a scheduler job and track completion |
| Missing notes or retrieval | Check capture and model errors separately; follow the [hook diagnostics](harness-adaptation.md#diagnose-opencode-hooks) |
| Changes to `opencode.json` disappear | Edit the persistent source described in step 5 |
| Scientific tool fails | Its underlying executable/environment may still need installing; follow its skill |

Do not restart a shared daemon during another person's run. Report the code revision,
OpenCode/Python versions, failing command, expected/actual result and a small redacted
log excerpt. Avoid uploading a whole notebook.

## 8. Record, update and back up

Record the working installation with the project, alongside scientific software versions:

```bash
git rev-parse HEAD
opencode --version
.venv/bin/python3 --version
.venv/bin/python3 -m pip freeze
```

Requirements have lower bounds, not a complete lockfile; OpenCode is also unpinned.
Record the working combination. Before updating, preserve local changes, back up the
project and review upstream changes. Reinstall both editable packages and repeat step 6.
Rolling back source does not roll back packages, client versions or project data.

The main repository does not back up ignored research data. The Git history inside
`.magnolia/` covers staged/confirmed entries, not all notebook files or run outputs.
Back up the **whole project**, including `.magnolia/`, inputs and runs, to a destination
approved for that research. Keep machine credentials separate.

Continue with [worked use cases](use-cases.md#two-worked-continuity-examples),
[domain adaptation](domain-adaptation.md), [memory](memory.md) or [cluster setup](hpc.md).
