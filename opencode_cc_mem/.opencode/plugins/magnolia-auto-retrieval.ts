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
 * session, append a clearly-marked directive instructing the agent to call
 * memory_get_context(task=...) before acting. The agent does the real MCP call —
 * so the result lands in context naturally AND the notice-queue drains.
 *
 * Safe by design: retrieval is READ-ONLY, so injecting this directive even on a
 * pure-discussion message cannot violate magnolia.md's propose-don't-act rule.
 * Any failure is swallowed — auto-retrieval must never break the opencode session.
 *
 * DUAL-SHAPE (2026-10-07, probe-verified on 1.18.34 + 2.0.6): plain-object
 * default export. v1 server() edits the first chat.message part (original
 * behavior — appending to the EXISTING part, since pushing a bare new part
 * fails sync validation, ref err_0520527a); v2 setup(ctx) appends to
 * event.prompt.text in the prompt-admission hook (migrate-v1 maps
 * chat.message -> prompt). v1.18.34 also calls setup() with a limited ctx —
 * guarded.
 *
 * Toggle: on by default (read-only, desired). Opt out with MAGNOLIA_AUTORETRIEVE=0|off.
 */
import { appendFileSync, mkdirSync, readFileSync } from "node:fs"
import { join, dirname } from "node:path"

const DISABLED = ["0", "off", "false", "no"].includes(
  String(process.env.MAGNOLIA_AUTORETRIEVE ?? "").toLowerCase(),
)

const DIRECTIVE =
  "\n\n[Magnolia · auto-memory] Before acting on this request, your FIRST tool " +
  "call must be `memory_get_context(task_description=\"<one-line summary of this " +
  "request>\")`. Exception: for a pure recap request (status / pending tasks / " +
  "where were we), answer from the SESSION HANDOVER already in your context and " +
  "skip the call. Otherwise the returned project entries and skill-tier rules " +
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

export default {
  id: "magnolia-auto-retrieval",

  /** opencode v2: prompt-admission hook — append the directive once per session. */
  async setup(ctx: any) {
    if (DISABLED) return
    if (!ctx?.session?.hook) return // v1.18.34 also calls setup() with a limited ctx — v1 runs server()
    const injected = new Set<string>()
    const path = logPath(ctx.location?.directory ?? process.cwd())
    const note = (sessionID: string) => {
      try {
        mkdirSync(dirname(path), { recursive: true })
        appendFileSync(
          path,
          JSON.stringify({ ts: new Date().toISOString(), sessionID, action: "inject" }) + "\n",
        )
      } catch { /* never throw into opencode */ }
    }
    await ctx.session.hook("prompt", async (event: any) => {
      try {
        const prompt = event?.prompt
        if (typeof prompt?.text !== "string" || !prompt.text) return
        const sessionID = (event as any)?.sessionID ?? "unknown"
        if (injected.has(sessionID)) return
        prompt.text = prompt.text + DIRECTIVE
        injected.add(sessionID)
        note(sessionID)
      } catch { /* never throw into opencode */ }
    })
  },

  /** opencode v1: original chat.message part-editing behavior. */
  async server({ directory }: any) {
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
      // We append the directive to the user's EXISTING text part — do NOT push
      // a new part. A bare new part lacks opencode's required aggregate fields
      // and is rejected by sync validation (EventV2.InvalidSyncEvent, ref
      // err_0520527a). The existing part already carries those fields.
      "chat.message": async (input: any, output: any) => {
        try {
          const sessionID = input?.sessionID
          if (!sessionID || injected.has(sessionID)) return
          if (!Array.isArray(output?.parts)) return
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
  },
}
