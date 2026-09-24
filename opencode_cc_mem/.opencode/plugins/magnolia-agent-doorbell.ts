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
 */
import { appendFileSync, mkdirSync, readFileSync } from "node:fs"
import { join, dirname } from "node:path"

import type { Plugin } from "@opencode-ai/plugin"

const DISABLED = String(process.env.MAGNOLIA_DOORBELL ?? "").toLowerCase() === "0"
const AGENTS = ["literature", "xiulian"]
const MENTION_RE = /@(literature|xiulian)\b[,:]?\s*([^\n]*)/gi

export const AgentDoorbell: Plugin = async ({ client, directory }) => {
  if (DISABLED) return {}

  // Current project, same resolution as magnolia-action-retrieval.
  let sourceProject = "unknown"
  try {
    const proj = readFileSync(join(directory, ".magnolia", ".active-project"), "utf8").trim()
    if (proj) sourceProject = proj
  } catch { /* fall through */ }

  function slug(text: string): string {
    const now = new Date()
    const hhmmss = now.toISOString().slice(11, 19).replace(/:/g, "")
    const words = text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()
      .split(/\s+/).slice(0, 4).join("-").slice(0, 40)
    return `${now.toISOString().slice(0, 10)}_${words || "request"}_${hhmmss}`
  }

  function deliver(agent: string, request: string): string {
    const taskDir = join(directory, "projects", agent, "inbox",
      `from-${sourceProject}`)
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

  return {
    "chat.message": async (input: any, output: any) => {
      try {
        if (!output?.parts || !Array.isArray(output.parts)) return
        for (const part of output.parts) {
          if (part?.type !== "text" || typeof part?.text !== "string") continue
          if (!/@(literature|xiulian)\b/i.test(part.text)) continue

          const sent: string[] = []
          const newText = part.text.replace(
            MENTION_RE,
            (match: string, agent: string, request: string, offset: number) => {
              const req = (request || "").trim()
              if (!req) return match // a bare @mention with no ask: leave it
              try {
                const file = deliver(agent.toLowerCase(), req)
                sent.push(`@${agent}`)
                return `[→ sent to @${agent} (letter ${file.split("/").pop()}; the agents daemon will run it)]`
              } catch {
                return match // delivery failed: leave the text untouched
              }
            },
          )
          if (sent.length > 0) {
            part.text = newText
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
}
