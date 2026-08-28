---
name: memory-setup
description: How to set up Magnolia's memory model when a user asks to "set up memory" — model roles, the magnolia memory command, verification, and the restart requirement.
version: 1.0
last_verified: 2026-08-28
tags: [setup, llm, memory-model, onboarding]
---

# Memory Model Setup (agent-driven step 2)

Magnolia uses **two LLM roles**, set in ONE place (`.magnolia/llm-setup.json`,
injected into `opencode.json` as `MAGNOLIA_MEMORY_MODEL` / `MAGNOLIA_MEMORY_PROVIDER`
by the `magnolia` launcher on every launch):

| Role | What it does | Guidance |
|---|---|---|
| **Main model** | Drives the agent the user talks to | User's choice; configured by `magnolia setup` (step 1) |
| **Memory model** | Background only: session distillation (~20 min timer + boot), rolling handover merge (boot), memory re-ranking, consolidation | **Cheap is right.** Default `deepseek-v4-flash`. Runs unattended, volume scales with activity |

They can be the same model. They usually should not be.

## The rule: never hand-edit opencode.json for LLM settings

`opencode.json` is **regenerated on every launch** from the template + the
side file — hand edits to it are wiped. The only durable controls are:

```bash
magnolia memory set <model> [--provider P]   # verify → save → enable → re-render
magnolia memory status                       # show side file + rendered state
magnolia setup                               # interactive both-steps (step 1 = main model)
```

The **provider is derived from the model name** (`deepseek-*` → deepseek,
`claude*` → anthropic, `gpt-*`/`o3-*`/`o4-*` → openai, `kimi-*`/`k3*` → kimi).
`--provider` is only for proxy/OpenAI-compatible endpoints where the name
doesn't identify the vendor. Do not ask the user for a provider.

## When memory setup is needed

Two triggers lead here — the flow below is the same for both:

1. **Automatic (the normal path)**: the memory-setup gate in AGENTS.md makes
   the agent check its tool list in its first response; missing
   `compchem-memory` tools → proactively offer setup, then follow this rule.
2. The user says "set up memory" (or `magnolia setup` deferred step 2).

1. **Explain the roles** (table above) in one short paragraph — cheap model
   for memory, smart model for the agent — and state the default
   (`deepseek-v4-flash`).
2. **Ask which memory model** they want, offering the default. Mention the
   quality alternative (`deepseek-v4-pro`) exists if their workload needs it.
3. **Check the key**: the chosen provider's API key must be in the
   environment (`DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `KIMI_API_KEY`). If the user pastes a key into chat, warn that chat
   content lands in session logs — prefer having them export it / put it in
   their env file themselves.
4. **Run the command**:
   ```bash
   magnolia memory set deepseek-v4-flash
   ```
   It makes one REAL test call before saving. On failure it writes nothing —
   report the stderr verbatim and fix the cause (wrong model id, missing
   key, network) before retrying. Do not bypass it by editing files.
5. **Confirm activation**:
   - `magnolia memory status` must show `memory enabled = True | model = <model>`.
   - Tell the user: **"Restart opencode to activate."** The running memory
     server keeps its launch-time settings until restarted — this is the one
     manual step that cannot be skipped.

## Why memory starts disabled

A half-configured memory server fails SILENTLY (heuristics instead of LLM)
for weeks — this actually happened (Aug 2026: an unrendered `@@DISTILL_MODEL@@`
frozen the handover for 3 weeks). The gate makes memory either fully
configured or not running. Never enable it without a passing verification.

## Fallbacks and edge cases

- No side file → launcher renders memory MCP disabled; the agent still works
  (memory tools are simply absent). Session capture keeps recording; when
  memory is later enabled it catches up on all past sessions.
- `magnolia setup` auto-migrates real values from a legacy `opencode.json`.
- Latency-sensitive jobs (re-ranking on `memory_get_context`) also use the
  memory model — a slow/expensive memory model taxes every task start.
