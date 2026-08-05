/**
 * magnolia-auto-retrieval — session-start memory retrieval trigger (v1, Architecture A)
 *
 * Spec: docs/superpowers/specs/2026-06-23-session-start-auto-retrieval.md
 *
 * Problem: the "call memory_get_context as your first action" rule is prose-only;
 * the agent can skip it, and then task-relevant project entries + skill-tier rules
 * (incl. domain rules once they move to the skill tier) never surface, and the
 * @captured notice-queue (pending consolidation/promotion proposals) never drains.
 *
 * v1 (Architecture A — directive injection): on the FIRST user message of a
 * session, append a clearly-marked directive part instructing the agent to call
 * memory_get_context(task=...) before acting. The agent does the real MCP call —
 * so the result lands in context naturally AND the notice-queue drains. This is
 * stronger than static prose (delivered at the exact moment, in-turn) but still
 * relies on the agent obeying; if compliance is poor, escalate to Architecture B
 * (plugin retrieves+injects content via a `magnolia-memory get-context` CLI).
 *
 * Safe by design: retrieval is READ-ONLY, so injecting this directive even on a
 * pure-discussion message cannot violate magnolia.md's propose-don't-act rule.
 * Any failure is swallowed — auto-retrieval must never break the opencode session.
 *
 * VERIFICATION STATUS: the hook FIRES and the part-mutation path IS live —
 * confirmed via a first-message test (opencode 1.17.9, glm-5.2): a v1 attempt that
 * PUSHED a new bare part reached opencode's sync layer but was rejected with
 * EventV2.InvalidSyncEvent "Expected string aggregate field sessionID" (ref
 * err_0520527a), so the directive was dropped. Fix: edit the EXISTING user text
 * part instead of pushing a new one. Still needs one live retest with a REAL task
 * (not "hello") to confirm the agent then calls memory_get_context. If editing the
 * existing part still doesn't reach the model, fall back to
 * `experimental.chat.messages.transform` (see FALLBACK below).
 *
 * Toggle: on by default (read-only, desired). Opt out with MAGNOLIA_AUTORETRIEVE=0|off.
 */
import { appendFileSync, mkdirSync, readFileSync } from "node:fs"
import { join, dirname } from "node:path"

import type { Plugin } from "@opencode-ai/plugin"

const DISABLED = ["0", "off", "false", "no"].includes(
  String(process.env.MAGNOLIA_AUTORETRIEVE ?? "").toLowerCase(),
)

const DIRECTIVE =
  "\n\n[Magnolia · auto-memory] Before acting on this request, your FIRST tool " +
  "call must be `memory_get_context(task_description=\"<one-line summary of this " +
  "request>\")`. Use the returned project entries and skill-tier rules to " +
  "inform your approach — accumulated learnings often hold the fix, the right " +
  "parameters, or a known pitfall for this exact task, and the longer the project " +
  "runs the more likely that is. (This call is read-only and safe even if the " +
  "turn is exploratory; it also surfaces any pending memory-review proposals.)"

// Resolve the project-pinned log path the same way session-capture does:
// magnolia writes the active project name to .magnolia/.active-project before
// exec-ing opencode (plugins don't receive shell env vars reliably).
function logPath(directory: string): string {
  try {
    const proj = readFileSync(join(directory, ".magnolia", ".active-project"), "utf8").trim()
    if (proj) return join(directory, "projects", proj, ".magnolia", "auto-retrieval.jsonl")
  } catch { /* fall through to root */ }
  return join(directory, ".magnolia", "auto-retrieval.jsonl")
}

export const MagnoliaAutoRetrieval: Plugin = async ({ directory }) => {
  if (DISABLED) return {}

  // Inject once per session (first user message). In-memory is sufficient: the
  // plugin closure lives for the opencode process; a re-inject after a plugin
  // reload is harmless (worst case the directive appears twice).
  const injected = new Set<string>()
  const path = logPath(directory)

  const note = (sessionID: string) => {
    try {
      mkdirSync(dirname(path), { recursive: true })
      appendFileSync(
        path,
        JSON.stringify({ ts: new Date().toISOString(), sessionID, action: "inject" }) + "\n",
      )
    } catch { /* never throw into opencode */ }
  }

  return {
    // `output.parts` is the documented mutable surface of chat.message.
    // We append a directive text part to the user's FIRST message so it rides the
    // same turn the agent processes — guaranteed before the agent acts, with no
    // session.prompt delivery-ordering race.
    "chat.message": async (input: any, output: any) => {
      try {
        const sessionID = input?.sessionID
        if (!sessionID || injected.has(sessionID)) return
        if (!Array.isArray(output?.parts)) return
        // Append the directive to the user's EXISTING text part — do NOT push a
        // new part. A bare new part lacks opencode's required aggregate fields and
        // is rejected by sync validation (EventV2.InvalidSyncEvent "Expected string
        // aggregate field sessionID", surfaced as ref err_0520527a on the first
        // message). The existing part already carries those fields, so editing its
        // .text propagates cleanly.
        let target: any = null
        for (let i = output.parts.length - 1; i >= 0; i--) {
          const p = output.parts[i]
          if (p?.type === "text" && typeof p?.text === "string") { target = p; break }
        }
        if (!target) return // nothing safe to edit; skip rather than risk a malformed part
        target.text = target.text + DIRECTIVE
        injected.add(sessionID)
        note(sessionID)
      } catch { /* never throw into opencode */ }
    },
  }
}

/*
 * FALLBACK (if chat.message output.parts does not reach the model):
 * replace the hook above with experimental.chat.messages.transform, which is the
 * explicit "modify messages sent to the LLM" surface:
 *
 *   "experimental.chat.messages.transform": async (_input: any, output: any) => {
 *     // append the DIRECTIVE as a text part on the latest user message in
 *     // output.messages, gated on first-occurrence per session.
 *   }
 */
