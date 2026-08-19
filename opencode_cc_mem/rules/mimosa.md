---
name: mimosa
description: How to run Mimosa-AI (agentic workflow framework over Toolomics MCP tools) for scientific campaigns — when to use it vs. direct Magnolia tools, prerequisites, run modes, and the protocol-fidelity checks required before letting an agent system touch a validated pipeline.
version: 1.0
last_verified: 2026-08-07
tags: [mimosa, toolomics, agents, mcp, workflow, automation]
---

# Mimosa-AI Operating Rules

Mimosa-AI (`~/repos/Mimosa_project/Mimosa-AI`) is a self-evolving multi-agent
framework. **It does not compute anything itself** — it synthesizes agent
workflows that call tools exposed by a running **Toolomics** MCP server
(`~/repos/Mimosa_project/toolomics`). Every Mimosa run is therefore only as
correct as (a) the tools Toolomics exposes and (b) the parameters the agents
choose. Both need verification before a campaign.

## When to use Mimosa vs. direct Magnolia tools

| Situation | Use |
|---|---|
| Exploratory, multi-step objective with unclear path (literature + data + compute mixed) | Mimosa `--goal` |
| New task type where no validated workflow exists yet — let it synthesize + evolve one | Mimosa `--goal --learn` |
| Single granular operation (one file conversion, one lookup) | Mimosa `--task`, or just Magnolia tools directly |
| **Validated/locked protocol where parameters must not drift** (e.g. a docking protocol whose scores are only comparable under exact flags) | Magnolia tools directly, or Mimosa only after the fidelity checks below pass |
| Production runs that need lifecycle tracking (`submit_job`, auto-poll, memory) | Magnolia `submit_job` — Mimosa bypasses all of it |

Rule of thumb: Mimosa explores; Magnolia executes validated protocols. Mimosa's
value is workflow *synthesis*, not parameter *discipline*.

## Prerequisites (check all, in order)

1. **Toolomics is running** — `cd ~/repos/Mimosa_project/toolomics && ./start.sh`.
   Mimosa discovers tools by scanning the port range in its config
   (`discovery_addresses`, default 5000–5200). If Toolomics is down, Mimosa
   starts and finds zero tools.
2. **The needed MCP servers are enabled** in the active Toolomics
   `config_*.json` (each has `"enabled": true/false`). Check before running —
   the config in the repo may not match what the campaign needs.
3. **`.env` exists in the Mimosa root** with the API key for the models named
   in `--config` (e.g. `moonshot/kimi-k2.5` needs its provider key).
4. **Inputs are staged in the Toolomics workspace** named by
   `workspace_dir` in the Mimosa config (e.g. `workspace_docking/`). Agents
   only see the workspace; absolute paths outside it may be blocked by the
   tools' path sanitizers. Stage copies, not symlinks.
5. **The config matches the campaign**: `my_config.json` controls workspace,
   models, runner limits (`runner_default_timeout` 7200 s default — long
   compute steps may need it raised).

## Run modes

```bash
cd ~/repos/Mimosa_project/Mimosa-AI

# Multi-step objective (planner mode) — campaigns, reproductions
uv run main.py --goal "<objective with explicit protocol spec>" --config my_config.json

# Single focused operation
uv run main.py --task "<one operation>" --config my_config.json

# Iterative learning — evolve the workflow over N iterations (new task types)
uv run main.py --goal "..." --learn --max_evolve_iterations 5 --config my_config.json

# Interactive tool debugging — test MCP tools by hand before a full run
uv run main.py --manual --config my_config.json

# Fast non-learning pass / verbose logs
uv run main.py --task "..." --single_agent --config my_config.json
uv run main.py --task "..." --debug --config my_config.json
```

Per the README: for any new task type, **start with `--learn`** so Mimosa
builds competence before full autonomy. For unbiased re-runs, `./cleanup.sh`
clears cached workflows (otherwise Mimosa reuses what it learned).

## Protocol fidelity — the mandatory pre-flight

Agent systems drift to tool defaults. Before any Mimosa run against a
validated protocol:

1. **Read the MCP tool's defaults** (`mcp_host/<tool>/server.py`) and diff
   them against the locked protocol. Real example: the `covalent_docking`
   MCP defaulted to the stub protocol (`[CD1]` SMARTS,
   `covalent_optimize=True`, seeds [0,42,123]) while the validated protocol is
   product-based (`[$([C](=C)-c)]`, `--cnn_scoring none`, no optimize-lig).
   Defaults that differ from the validated protocol silently produce
   non-comparable results.
2. **Embed the full protocol spec in the goal/task prompt** — exact flags,
   parameter values, file names, acceptance criteria. Agents fill unspecified
   gaps with tool defaults or their own judgment.
3. **Pilot before scaling**: run 1–2 compounds with known expected results
   through Mimosa first and check reproduction (scores within protocol noise,
   geometric sanity checks pass). Only scale to the full batch if the pilot
   matches. This mirrors the re-docking validation done for the GPX4
   protocol (2026-08-07: 5 ligands reproduced within 0.4 kcal/mol).
4. If a tool's defaults are wrong for the protocol, **patch the tool or pin
   the parameters in the prompt** — do not assume the agent will override
   them correctly every iteration.

## Outputs, audit trail, and Magnolia integration

- Live artifacts: Toolomics `workspace*` dir. Archived snapshot per run:
  `runs_capsule/<name>/`. Workflow metadata + agent traces:
  `sources/workflows/<uuid>/` (replay with `memory_explorer.py <uuid>`).
- **Mimosa runs bypass Magnolia lifecycle tracking** (no run YAML, no
  auto-assess). After a Mimosa campaign:
  1. Copy result artifacts into `projects/<name>/runs/YYYY-MM-DD_<name>/`
     (per `rules/job_execution.md` RULE 1).
  2. Call `memory_record_run` manually so the run appears in project history.
  3. Call `post_run_assess` on the copied directory if the tool is recognized.
  4. Record learnings (`success_pattern` / `failure_pattern`) with the
     quantitative results — Mimosa's own `sources/memory/` is not visible to
     Magnolia memory.
- Conversely, Magnolia memory entries are not visible to Mimosa — put any
  hard-won parameter lessons directly into the Mimosa prompt or the MCP
  tool's defaults.

## Common mistakes

| Mistake | Correct |
|---|---|
| Running Mimosa with Toolomics down | Always `./start.sh` Toolomics first; Mimosa finds zero tools otherwise |
| Assuming the needed MCP server is enabled | Check the active `config_*.json` `enabled` flags |
| Vague goal prompt ("dock these compounds") | Embed the exact protocol: flags, params, file paths, acceptance criteria |
| Trusting tool defaults to match the validated protocol | Diff defaults vs. protocol; patch the tool or pin params in the prompt |
| Scaling to a full batch without a pilot | Pilot 1–2 known compounds, verify reproduction, then scale |
| Expecting Mimosa runs in Magnolia run history | They bypass `submit_job`; copy outputs to `runs/` and `memory_record_run` manually |
| Stale learned workflows contaminating a fresh evaluation | `./cleanup.sh` before unbiased re-runs |
| Letting agents read inputs outside the workspace | Stage copies inside `workspace_dir` |

## When this rule is wrong

- Mimosa/Toolomics change their CLI, config schema, or port-discovery
  mechanism — re-check against the upstream READMEs.
- A future integration registers Magnolia's compchem-tools as a Toolomics MCP
  server — then the "two separate worlds" sections (lifecycle, memory) need
  revisiting.
