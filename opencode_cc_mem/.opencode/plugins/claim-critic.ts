/**
 * claim-critic — FLAG-ONLY claim-vs-action auditor (Stage 1, observe only).
 *
 * When a Magnolia turn goes idle, an independent judge LLM reads the assistant's
 * report PLUS what the agent actually DID this turn (tool calls + their real
 * outputs) and flags any claim not supported by those actions — e.g. a computed
 * number / energy / ranking / mechanism that no tool actually produced, or
 * analysis steps the report implies but the trace shows were skipped.
 *
 * It changes NOTHING in the session: it only appends a verdict to
 *   <project>/.magnolia/claim-critic/<sessionID>.jsonl
 * and shows a session-scoped TUI toast when something is flagged. No blocking,
 * no edits, and no critic text in model context.
 *
 * Enable per-session by exporting MAGNOLIA_CRITIC=1 (the `magnolia --critic`
 * wrapper flag does this). Without it the plugin is a no-op.
 *
 * JUDGE PROVIDER (2026-11-15). Resolution mirrors the memory client
 * (compchem_memory/llm.py):
 *   1. MAGNOLIA_CRITIC_PROVIDER — deepseek | openai | anthropic | kimi |
 *      openrouter | mistral | zai;
 *   2. otherwise the prefix of MAGNOLIA_CRITIC_MODEL (deepseek*, claude*,
 *      gpt-, kimi, mistral, devstral and the other Mistral families, glm,
 *      and vendor/model ids → openrouter);
 *   3. otherwise the first key present, DeepSeek first (the historical
 *      behaviour), then Anthropic, OpenAI, Kimi, OpenRouter, Mistral, Z.ai.
 * MAGNOLIA_CRITIC_MODEL overrides the provider's default model,
 * MAGNOLIA_CRITIC_API_KEY its key and MAGNOLIA_CRITIC_BASE_URL its endpoint
 * (any OpenAI-compatible service through provider "openai"). The provider
 * base-URL variables of the memory client (DEEPSEEK_BASE_URL, ZAI_BASE_URL, …)
 * are honoured too. A judge from another model family than the main agent
 * keeps the audit independent. Failed judge calls are appended to
 * <claim-critic dir>/errors.jsonl so a misconfigured critic is visible.
 *
 * Why a judge that sees the tool OUTPUTS (not just summaries): validated in
 * projects/probability_entropy/derisk_judge.py — with only summaries the judge
 * over-flags legitimate computed values; with the real outputs it cleanly passes
 * good reports and flags skipped-steps garbage.
 *
 * Defensive by design: any failure is swallowed so auditing can never break the
 * opencode session.
 *
 * DUAL-SHAPE (2026-10-07, probe-verified on 1.18.34 + 2.0.6): plain-object
 * default export; v2 setup(ctx) subscribes to the event stream; v1 server()
 * keeps the original event hook. v1.18.34 also calls setup() with a limited
 * ctx — guarded. v2 has no server-side TUI domain: flagged verdicts degrade
 * to stderr (the JSONL log is the primary record either way).
 *
 * OpenCode 2.3.0 (probed 2026-11-14): loads. Not yet observed acting in the
 * interactive TUI. Headless `run` exits before event-started async work completes.
 *
 * M2 therefore persists and detaches synchronously at the terminal event;
 * transcript capture, judging, idempotence, and retries belong to Python.
 */
import { eventTurnDetails, handoffTurnEnd } from "../lib/turn-end-worker.ts"

const ENABLED = !!process.env.MAGNOLIA_CRITIC

function isTurnEnd(event: any): boolean {
  return event?.type === "session.idle" || event?.type === "session.execution.succeeded"
}

function sessionID(event: any): string {
  return String(event?.durable?.aggregateID ?? event?.properties?.sessionID ?? event?.sessionID ?? "")
}

export default {
  id: "claim-critic",

  async setup(ctx: any) {
    if (!ENABLED || !ctx?.event?.subscribe) return
    const directory = ctx.location?.directory ?? process.cwd()
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const sid = sessionID(event)
          if (isTurnEnd(event) && sid) {
            handoffTurnEnd(directory, sid, "critic", eventTurnDetails(event))
          }
        }
      } catch { /* aborted or stream error */ }
    })()
    return () => controller.abort()
  },

  async server({ directory }: any) {
    if (!ENABLED) return {}
    return {
      event: (input: any) => {
        const event = input?.event
        const sid = sessionID(event)
        if (isTurnEnd(event) && sid) {
          handoffTurnEnd(directory, sid, "critic", eventTurnDetails(event))
        }
      },
    }
  },
}
