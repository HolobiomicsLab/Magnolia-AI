/**
 * claim-critic — detached handoff for the Python hook-core critic.
 *
 * OpenCode `run` does not await asynchronous work started from its turn-end
 * event. The handler therefore persists and spawns synchronously; transcript
 * capture, judging, idempotence, and later notices belong to the hook core.
 */
import { handoffTurnEnd } from "../lib/turn-end-worker.ts"

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
          if (isTurnEnd(event) && sid) handoffTurnEnd(directory, sid, "critic")
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
        if (isTurnEnd(event) && sid) handoffTurnEnd(directory, sid, "critic")
      },
    }
  },
}
