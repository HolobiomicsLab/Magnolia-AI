# Session-start auto-retrieval trigger

**Status:** v1 BUILT (Architecture A) — transpiles clean; runtime propagation needs a live test
**Date:** 2026-06-23

**v1 artifact:** `.opencode/plugins/magnolia-auto-retrieval.ts` (registered in
opencode.json + .template). On the first user message it appends a directive part
to `chat.message`'s `output.parts` telling the agent to call `memory_get_context`
first; logs each injection to `<project>/.magnolia/auto-retrieval.jsonl`. Default-on
(opt out `MAGNOLIA_AUTORETRIEVE=0`). **Live test still required** — see below.

**Spike result:** U1 (injection) = **YES** via `client.session.prompt`. U2 (CLI
retrieval) = CLI exists, needs a small `get-context` subcommand — **only for
Architecture B**. ⇒ **Architecture A can be built now with no prerequisite infra.**
**Prerequisite for:** [2026-06-23-lazy-rule-loading-via-skill-tier.md] — the rule
move can't safely land until task-relevant retrieval reliably fires, or moved
domain skills go from *buried* to *absent*.

## Goal

Make `memory_get_context(task=…)` reliably run **early in every working session
with a task-relevant query**, instead of relying on the prose "call it as your
first action" rule (which the agent can skip). This:
1. Guarantees task-relevant **project entries + skill-tier rules** are surfaced
   (the moved haddock3/gromacs/etc. skills ride on this call's `assemble_context`).
2. Implements the "search memory before acting" intent from the proactivity
   discussion.
3. **Bonus:** the call drains the `@captured` `.distill-notices` queue, so any
   pending consolidation/promotion proposals surface at session start too —
   fixing the notice chicken-and-egg (notices only surface on a memory-tool call).

## The hard constraint

Task-specific retrieval needs the **task**, which first appears in the user's
**first message** — not at boot. The MCP server never sees that message. Only an
opencode **plugin** sits at that point. So this must be a plugin (extend
`magnolia-session-capture.ts`, which already hooks `chat.message`, or a sibling).

## Verified plugin API (from claim-critic.ts / session-capture.ts)

Confirmed available to a plugin:
- Hooks: `chat.message` (fires per user message, carries `sessionID`), `event`
  (lifecycle bus: `session.idle`, etc.).
- `client.session.messages({path:{id}})` — read the transcript.
- `client.tui.showToast(...)` — surface a toast.
- `fetch(...)` — call external HTTP (claim-critic calls DeepSeek directly).
- Per-project pinning via `process.env.MAGNOLIA_PROJECT_DIR`.

**Two unknowns — RESOLVED by spike (2026-06-23):**
- **U1 — injection: YES.** `@opencode-ai/sdk` exposes `client.session.prompt(...)`
  — docstring "Create a v2 session message and **queue it for the agent loop**";
  params `sessionID`, `directory`, `workspace`, `prompt` (body), **`delivery`**
  (body). So a plugin can inject a message the agent processes. (`appendPrompt` /
  `submitPrompt` also exist at TUI level.) **Architecture A needs nothing more.**
  - *Residual build nuance (not a blocker):* `session.prompt` *queues* a message,
    so we must use `delivery` (and/or fire timing) to ensure the injected directive
    lands **before** the agent acts on the user's task, not interleaved after it.
- **U2 — plugin-side retrieval: CLI exists, subcommand must be added.** There is a
  `magnolia-memory` console script (`compchem_memory.cli:main`), but its
  subcommands are log-bash / assess / log-job / log-event / sync-queue / init-vault
  / generate-daily-note / compact-session — **no `get-context`.** The retrieval
  functions (`assemble_context`, `select_relevant_{entries,skills}`) are standalone
  and importable, so adding a `get-context` subcommand is ~15 lines. **Only needed
  for Architecture B; Architecture A does not need it** (the agent calls the MCP
  tool it already has).

## Two architectures

**A — Directive injection (minimal).** On the first user message, inject a
synthetic instruction: *"Before responding, call `memory_get_context(task=<the
message>)` and use the results."* Relies on **U1** only. Stronger than static
prose (delivered at the exact moment, unmissable), but still depends on the agent
obeying — the agent does the actual retrieval (so the result lands in context
naturally, and the notice-queue drains).

**B — Retrieve-and-inject (guaranteed).** Plugin retrieves memory itself (via CLI
shell-out per **U2**) and injects the *content* (per **U1**) so it's present
regardless of agent compliance. Strongest guarantee; most moving parts; risks
double-retrieval if the agent also calls the tool; injecting a large block may
clutter the turn.

## Recommendation — staged

1. **Verify U1 and U2 first** (a 30-minute spike: can a plugin inject an
   actionable message? is there a `get-context` CLI?). The answers pick the path.
2. **Ship Architecture A as v1** — lowest risk, smallest change, and it makes the
   agent do the real call (natural context delivery + notice drain).
3. **Measure compliance** — log (like claim-critic does) whether `get_context`
   actually ran within the first N tool calls per session.
4. **Escalate to B only if A's compliance is poor.** Don't build the heavier path
   until data shows the directive isn't enough.

## Design details (apply to whichever architecture)

- **Fire policy:** once per session, on the **first** `chat.message`. Task-shift
  mid-session (a genuinely new task) is deferred to v2 — the prose rule + the
  submit_job gate (below) cover the gap. (Re-firing on every message is wrong:
  cost + noise.)
- **Query:** the user's first message text as `task_description`, length-capped.
  `get_context`'s heuristic scorer handles free text.
- **Discussion-safe — no execution gating needed.** Per magnolia.md, retrieval is
  read-only and *always allowed during discussion*. So auto-firing on any first
  message (even a pure question) is safe; unlike record/confirm, it needs no
  go-ahead. This is why retrieval is the right thing to automate and writes are not.
- **Never break the session.** Swallow all errors (mirror claim-critic's
  try/catch-all). A retrieval failure must not block the turn.
- **Config:** a flag (default **on**; opt-out env var), mirroring `MAGNOLIA_CRITIC`.
- **Pinning:** use `MAGNOLIA_PROJECT_DIR` so retrieval targets the pinned project.
- **De-dup:** if the agent also calls `get_context` (prose rule), Architecture A
  yields one call (agent's); B may double — gate B on "has get_context run yet?"

## Complementary, not in scope here

The high-stakes path wants a **tool-level gate** independent of session start:
`submit_job` queries memory for matching prior failures / param-guidance at submit
time (generalizing the existing `apply_tool_memory_floor`), surfacing or
fail-closing on known mechanical failures. That covers "search before the
expensive action" even if session-start retrieval was skipped. Tracked separately.

## Verification
- A docking session: `memory_get_context` runs within the first turn; its result
  contains the haddock3 skill (post-rule-move); pending proposals (if any) appear
  via the drained notice.
- A pure-discussion first message still triggers retrieval and does **not** mutate
  anything.
- Compliance log shows `get_context` firing rate ≈ 100% (A) across real sessions.

## Rollback
Plugin-only; remove the plugin (or flip the flag off) to revert. No server/code
changes if Architecture A. CLI shell-out (if B) is additive.

## Open questions for sign-off
1. Resolve **U1** (injection API) and **U2** (CLI vs MCP retrieval) via the spike —
   blocks architecture choice.
2. Confirm **Architecture A first**, escalate to B only on poor compliance.
3. Default-on vs opt-in flag for v1? (recommend default-on; it's read-only.)
