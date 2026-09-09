# Architecture

Magnolia is not a single programme but a small assembly of parts, joined by the
[Model Context Protocol](https://modelcontextprotocol.io). This document describes what each part
does, why two of the arrangements are unusual, and what happens over the course of a session.

## The four components

| Layer | Component | Location | Function |
|---|---|---|---|
| Interface | OpenCode | external | The terminal client; brokers the model, loads instructions, renders the conversation |
| Execution | **compchem-tools** | `opencode_cc_mem/mcp-servers/compchem-tools/` | 36 typed tools: scientific instruments, structure preparation, Slurm |
| Memory | **compchem-memory** | `opencode_cc_mem/mcp-servers/compchem-memory/` | 24 tools: capture, retrieval, distillation, consolidation, promotion |
| Knowledge | rules, skills, notebook | `rules/`, `.opencode/skills/`, `<project>/.magnolia/` | What Magnolia knows before, during and after a session |

A fifth is optional: [Perspicacité](https://github.com/HolobiomicsLab/Perspicacite-AI), a literature
retrieval server declared on `localhost:8000`. When it is running it is picked up automatically;
when it is not, the corresponding tools are simply absent.

## Two arrangements that require explanation

### compchem-tools runs as an HTTP daemon

The client declares it as a *remote* server on `127.0.0.1:8001` rather than spawning it over
standard input and output. The reason is a failure mode rather than a preference: when a tool call
is aborted, the client closes its side of the pipe; a locally spawned server observes a near-instant
disconnection, its watchdog concludes that the client has gone, and it exits — and the client does
not respawn a dropped local server. Every scientific tool then vanishes for the remainder of the
session, silently.

As an HTTP server the process outlives client aborts, and
[`compchem-tools-daemon.sh`](../opencode_cc_mem/softwares/bin/compchem-tools-daemon.sh) supervises
it and restarts it if it dies. The `magnolia` launcher starts the daemon before executing the
client; the operation is idempotent and effectively instantaneous when it is already running.

Liveness is determined from the supervisor's own process, not from the port: a transparent proxy can
answer for `127.0.0.1` when nothing is listening locally, which would otherwise produce a false
"already running" that prevents the daemon from ever starting.

### The general shell is disabled

OpenCode's own `bash` tool is switched off in the configuration template. Commands therefore reach
the system through `run_shell`, which records them, or through `magnolia-run`, which wraps a command
you invoke yourself and logs it to the session journal. The purpose is not restriction but
provenance: a command that leaves no trace cannot be part of the record, and the record is the
product.

## What is loaded at the start of a session

The launcher renders `opencode.json` from its template on every launch — substituting the project,
the interpreter and the model choices — and the client then loads, in order:

1. `rules/*.md` — doctrine, read every session. Eight files at present, covering agent behaviour,
   job execution, pre-submission verification, Slurm, memory setup, Overleaf and the per-cluster
   template. Every line is paid for in every session, which is the reason this directory is kept
   short.
2. `AGENTS.md` — the memory protocol: when to call which memory tool, and the setup gate that
   obliges Magnolia to tell you if memory is not configured rather than proceeding quietly.
3. `<project>/.magnolia/boot-context.md` — the project's standing context, written by memory.
4. `<project>/.magnolia/audit-report.md` — outstanding observations about the project's own record.

Skills are **not** loaded at the start. Each carries a description, and the model loads
`SKILL.md` only when the task at hand matches it — which is why a tool-specific protocol can be as
long as it needs to be, whereas a rule cannot.

Four plugins run alongside: session capture, automatic retrieval, action retrieval, and the
claim critic (enabled per session with `--critic`).

## The course of a session

```
launch ──> daemon up ──> rules + AGENTS.md + boot context loaded
                              │
        you state a task ─────┤
                              ▼
                    memory_get_context      retrieve what is relevant to this task
                              ▼
                       skill loaded         if the task matches one
                              ▼
                   proposal, then act       tools run; outputs to runs/YYYY-MM-DD_name/
                              ▼
                   capture as it happens    session journal, run records, errors
                              ▼
              distillation and staging      at session end, or by magnolia-selfreflex
                              ▼
                    promotion proposal      after corroboration; awaits your decision
```

The left-hand column is automatic; the two decisions — whether to act, and whether a learning
becomes a rule — are yours. See [memory.md](memory.md) for the second.

## Where things are written

| Path | Content | Tracked by git |
|---|---|---|
| `opencode_cc_mem/rules/` | Doctrine, including promoted rules | Yes |
| `opencode_cc_mem/.opencode/skills/` | Task protocols | Yes |
| `opencode_cc_mem/projects/<name>/raw_input/` | Your inputs | No |
| `opencode_cc_mem/projects/<name>/runs/` | One dated directory per computation | No |
| `opencode_cc_mem/projects/<name>/.magnolia/` | The notebook | No |
| `opencode_cc_mem/softwares/` | Scientific software and launchers | Only the small infrastructure scripts |
| `opencode_cc_mem/logs/` | Daemon logs | No |
| `~/.config/magnolia/clusters.yaml` | Your personal cluster facts | No — outside the repository by design |

The asymmetry is deliberate. The repository carries the assistant; the science stays with the
scientist, and anything specific to one machine or one person stays off the shared history.

## Design principles

- **Provenance is not a feature added afterwards.** Every mechanism above — the disabled shell, the
  mandatory run directories, the session journal — exists so that a result can be traced to the
  command that produced it without Magnolia being present to explain.
- **Machine-legible where it must be, human-legible where it can be.** The notebook is Markdown,
  browsable in an editor or as an Obsidian vault; the promotion proposal is JSON because it is a
  queue.
- **Nothing durable is written without a person.** A rule is a standing instruction for every future
  session; it is therefore proposed, reviewed and accepted, never merely inferred.
