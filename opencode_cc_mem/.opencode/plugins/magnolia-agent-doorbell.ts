/**
 * magnolia-agent-doorbell — "@literature ..." / "@xiulian ..." trigger.
 *
 * When a user message mentions one of the helper agents, the rest of that
 * line becomes a request letter: the plugin writes
 *   <root>/projects/<agent>/inbox/from-<this-project>/<date>_<slug>.task.md
 * (status: open), which the magnolia-agents-daemon picks up and runs
 * headlessly. The @mention in the user's message is replaced with a short
 * "[sent to @agent]" note so the local session does not act on it.
 *
 * Kill switch: MAGNOLIA_DOORBELL=0. Defensive by design: any failure is
 * swallowed so the doorbell can never break a session.
 *
 * Design: docs/agents-daemon-design.md
 *
 * DUAL-SHAPE (2026-10-07, probe-verified on 1.18.34 + 2.0.6): plain-object
 * default export. v1 server() edits the chat.message output parts (original
 * behavior); v2 setup(ctx) uses ctx.session.hook("prompt") — the prompt
 * hook's event.prompt.text is the mutable admitted draft, and edits become
 * the canonical persisted input (migrate-v1 maps chat.message -> prompt).
 * v1.18.34 also calls setup() with a limited ctx — guarded.
 *
 * REGISTRY (2026-10-07): the mention set is the default pair UNION every
 * project that declares projects/<name>/agent.json (enabled != false). New
 * agents need no code edit — registering the project is enough.
 */
import { appendFileSync, mkdirSync, readFileSync, readdirSync, existsSync } from "node:fs"
import { join, dirname } from "node:path"

const DISABLED = String(process.env.MAGNOLIA_DOORBELL ?? "").toLowerCase() === "0"
const DEFAULT_AGENTS = ["literature", "xiulian"]

function registryAgents(directory: string): string[] {
  const names = new Set(DEFAULT_AGENTS)
  try {
    for (const d of readdirSync(join(directory, "projects"), { withFileTypes: true })) {
      if (!d.isDirectory()) continue
      const cfgPath = join(directory, "projects", d.name, "agent.json")
      if (!existsSync(cfgPath)) continue
      try {
        const cfg = JSON.parse(readFileSync(cfgPath, "utf8"))
        if (cfg && cfg.enabled === false) continue
      } catch { continue }
      names.add(d.name)
    }
  } catch { /* fall through */ }
  return [...names]
}

function buildMentionRes(agents: string[]): { mention: RegExp; check: RegExp } {
  const esc = agents.map((a) => a.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
  return {
    mention: new RegExp(`@(${esc.join("|")})\\b[,:]?\\s*([^\\n]*)`, "gi"),
    check: new RegExp(`@(${esc.join("|")})\\b`, "i"),
  }
}

type Toast = (message: string) => void

function sourceProjectOf(directory: string): string {
  try {
    const proj = readFileSync(join(directory, ".magnolia", ".active-project"), "utf8").trim()
    if (proj) return proj
  } catch { /* fall through */ }
  return "unknown"
}

function slug(text: string): string {
  const now = new Date()
  const hhmmss = now.toISOString().slice(11, 19).replace(/:/g, "")
  const words = text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()
    .split(/\s+/).slice(0, 4).join("-").slice(0, 40)
  return `${now.toISOString().slice(0, 10)}_${words || "request"}_${hhmmss}`
}

function deliver(directory: string, sourceProject: string, agent: string, request: string): string {
  const taskDir = join(directory, "projects", agent, "inbox", `from-${sourceProject}`)
  mkdirSync(taskDir, { recursive: true })
  const file = join(taskDir, `${slug(request)}.task.md`)
  const letter = [
    `# Task: ${request.slice(0, 80)}`,
    ``,
    `- from: ${sourceProject}`,
    `- date: ${new Date().toISOString()}`,
    `- status: open`,
    ``,
    `## Request`,
    ``,
    request.trim() || "(see attached session context; ask the sender if unclear)",
    ``,
  ].join("\n")
  mkdirSync(dirname(file), { recursive: true })
  appendFileSync(file, letter + "\n")
  return file
}

/** Shared mention-processing: returns rewritten text + delivered agents. */
function processText(
  directory: string, sourceProject: string, text: string,
  mentionRe: RegExp, checkRe: RegExp,
): { text: string; sent: string[] } {
  const sent: string[] = []
  if (!checkRe.test(text)) return { text, sent }
  const newText = text.replace(
    mentionRe,
    (match: string, agent: string, request: string, _offset: number) => {
      const req = (request || "").trim()
      if (!req) return match // a bare @mention with no ask: leave it
      try {
        deliver(directory, sourceProject, agent.toLowerCase(), req)
        sent.push(`@${agent}`)
        return `[→ sent to @${agent}; the agents daemon will run it]`
      } catch {
        return match // delivery failed: leave the text untouched
      }
    },
  )
  return { text: sent.length > 0 ? newText : text, sent }
}

function toastFor(toast: Toast, agent: string) {
  toast(`${agent} triggered — request letter written; the daemon will run it.`)
}

export default {
  id: "magnolia-agent-doorbell",

  /** opencode v2: prompt-admission hook (chat.message equivalent). */
  async setup(ctx: any) {
    if (DISABLED) return
    if (!ctx?.session?.hook) return // v1.18.34 also calls setup() with a limited ctx — v1 runs server()
    const directory = ctx.location?.directory ?? process.cwd()
    const sourceProject = sourceProjectOf(directory)
    const { mention, check } = buildMentionRes(registryAgents(directory))
    const toast: Toast = (message) => {
      const t = (ctx as any)?.tui?.showToast
      if (typeof t === "function") {
        t.call((ctx as any).tui, { body: { title: "agents-doorbell", message } }).catch(() => {})
      } else {
        console.error(`[agents-doorbell] ${message}`)
      }
    }
    await ctx.session.hook("prompt", async (event: any) => {
      try {
        const prompt = event?.prompt
        if (typeof prompt?.text !== "string" || !prompt.text) return
        const { text, sent } = processText(directory, sourceProject, prompt.text, mention, check)
        if (sent.length > 0) {
          prompt.text = text
          for (const agent of sent) toastFor(toast, agent)
        }
      } catch { /* never throw into opencode */ }
    })
  },

  /** opencode v1: original chat.message part-editing behavior. */
  async server({ client, directory }: any) {
    if (DISABLED) return {}
    const sourceProject = sourceProjectOf(directory)
    const { mention, check } = buildMentionRes(registryAgents(directory))
    return {
      "chat.message": async (_input: any, output: any) => {
        try {
          if (!output?.parts || !Array.isArray(output.parts)) return
          for (const part of output.parts) {
            if (part?.type !== "text" || typeof part?.text !== "string") continue
            const { text, sent } = processText(directory, sourceProject, part.text, mention, check)
            if (sent.length > 0) {
              part.text = text
              for (const agent of sent) {
                client.tui
                  .showToast({
                    body: {
                      title: "agents-doorbell",
                      message: `${agent} triggered — request letter written; the daemon will run it.`,
                    },
                  })
                  .catch(() => {})
              }
            }
          }
        } catch { /* never throw into opencode */ }
      },
    }
  },
}
