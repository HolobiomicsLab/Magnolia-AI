---
name: memory-setup
description: How to set up Magnolia's memory model when a user asks to "set up memory" — model roles, the magnolia memory command, verification, and the restart requirement.
version: 1.1
last_verified: 2026-11-17
tags: [setup, llm, memory-model, onboarding]
---

# Memory Model Setup (agent-driven step 2)

Magnolia uses **two LLM roles**, set in ONE place (`.magnolia/llm-setup.json`,
injected into `opencode.json` as `MAGNOLIA_MEMORY_MODEL` / `MAGNOLIA_MEMORY_PROVIDER`
and `MAGNOLIA_CRITIC_MODEL` / `MAGNOLIA_CRITIC_PROVIDER` by the `magnolia`
launcher on every launch):

| Role | What it does | Guidance |
|---|---|---|
| **Main model** | Drives the agent the user talks to | User's choice; configured by `magnolia setup` (step 1) |
| **Memory model** | Background only: session distillation (~20 min timer + boot), rolling handover merge (boot), memory re-ranking, consolidation | **Cheap is right.** New setup prefers OpenRouter `deepseek/deepseek-v4.1-flash` when `OPENROUTER_API_KEY` is available; otherwise it keeps direct `deepseek-flash`. |

They can be the same model. They usually should not be.
For a new setup the critic starts on the same explicit provider/model pair as
memory. Existing memory or critic choices are preserved.

## The rule: never hand-edit opencode.json for LLM settings

`opencode.json` is **regenerated on every launch** from the template + the
side file — hand edits to it are wiped. The only durable controls are:

```bash
magnolia memory set <model> [--provider P]   # verify → save → enable → re-render
magnolia memory set <model> --provider openrouter \
  --fallback-provider deepseek --fallback-model deepseek-flash
magnolia memory status                       # show side file + rendered state
magnolia setup                               # interactive both-steps (step 1 = main model)
```

The launcher persists both provider and model explicitly. The provider is derived from the selected model name (`vendor/model` → OpenRouter, `deepseek-*` → deepseek,
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
   (`deepseek/deepseek-v4.1-flash` through OpenRouter when its key is available,
   otherwise direct `deepseek-flash`). Existing configurations are never rewritten.
2. **Ask which memory model** they want, offering the default. Mention the
   quality alternative (`deepseek-v4-pro`) exists if their workload needs it.
3. **Check the key**: the chosen provider's API key must be in the
   environment (`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`, `KIMI_API_KEY`, `MISTRAL_API_KEY`, `ZHIPU_API_KEY` or
   `ZAI_API_KEY`). Setup names the key variable it used. If the user pastes a key into chat, warn that chat
   content lands in session logs — prefer having them export it / put it in
   their env file themselves.
4. **Run the command**:
   ```bash
   # With OPENROUTER_API_KEY:
   magnolia memory set deepseek/deepseek-v4.1-flash
   # Otherwise, with DEEPSEEK_API_KEY:
   magnolia memory set deepseek-flash
   ```
   It makes one REAL test call before saving. On failure it writes nothing —
   report the stderr verbatim and fix the cause (wrong model id, missing
   key, network) before retrying. Do not bypass it by editing files.
5. **Confirm activation**:
   - `magnolia memory status` must show `memory enabled = True | model = <model>`.
   - Tell the user: **"Restart the agent client to activate."** The running memory
     server keeps its launch-time settings until restarted — this is the one
     manual step that cannot be skipped.

### OpenRouter routing controls

OpenRouter calls use a role-specific, project-private sticky session id by default and apply a quality
floor of fp8, bf16, fp16, fp32 or unknown quantization. Two optional environment variables control this:

- `MAGNOLIA_OPENROUTER_PROVIDER` — a JSON provider object that replaces the quality floor entirely, for
  example a manual provider order. Invalid JSON is ignored with one stderr warning.
- `MAGNOLIA_OPENROUTER_STICKY=0` — omit the sticky session id (`false`, `no` and `off` also disable it).

No project path is sent to OpenRouter: the session id contains only the LLM role and the first 16 hex
characters of a SHA-256 digest of the resolved project directory.

The OpenCode TypeScript claim-critic plugin only hands work off. The Python
critic owns provider routing, sticky sessions, secret scrubbing and usage
accounting. Critic prompts have no prefix reused across calls, so the critic
does not set a cache prefix.

An explicit OpenRouter route never silently borrows another provider's key.
Missing keys, HTTP 429, outages and 5xx responses produce a typed error,
telemetry and one visible notice per session, then memory uses its existing
heuristic path and the critic is disabled for that attempt. A direct fallback
is used only when both `direct_fallback_provider` and
`direct_fallback_model` are set by the optional `magnolia memory set`
fallback flags in `.magnolia/llm-setup.json`; the launcher
passes them as `MAGNOLIA_DIRECT_FALLBACK_PROVIDER` and
`MAGNOLIA_DIRECT_FALLBACK_MODEL`. The default is unset.

The managed memory service admits API-key variables only for the selected
memory provider, an explicitly configured critic provider and an explicitly
configured direct-fallback provider. A critic failure on any configured
provider queues one visible unavailable notice per session and writes its
typed error row; this notice policy is not limited to OpenRouter.

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
