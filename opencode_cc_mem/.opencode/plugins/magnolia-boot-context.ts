/**
 * magnolia-boot-context — opencode v2 context-hook port.
 *
 * v1 loaded boot-context.md, audit-report.md and rules/*.md through the
 * opencode `instructions` array. opencode v2.0.6 parses that field but only
 * loads AGENTS.md, so the launcher drops it in v2 mode. This plugin supplies
 * the same text through ctx.session.hook("context"), which runs immediately
 * before each agent model request.
 *
 * DUAL-SHAPE: v1 calls server() (not defined here) and this setup() is a no-op
 * when ctx.session.hook is absent. No @opencode/plugin import is needed, so it
 * loads even though that package is not installed in .opencode/node_modules.
 *
 * Session binding: the project is resolved from .magnolia/.active-project at
 * the session's first context call and cached per session id. A later launch
 * flipping the marker must not switch an existing conversation's boot context.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs"
import { basename, dirname, join } from "node:path"

const MAX_PAYLOAD_CHARS = 256_000
const cache = new Map<string, { key: string; text: string }>()

function readCached(path: string): string {
  try {
    const st = statSync(path)
    const key = `${st.mtimeMs}:${st.size}`
    const hit = cache.get(path)
    if (hit && hit.key === key) return hit.text
    const text = readFileSync(path, "utf8")
    cache.set(path, { key, text })
    return text
  } catch {
    return ""
  }
}

function activeProject(directory: string): string | null {
  try {
    const proj = readFileSync(join(directory, ".magnolia", ".active-project"), "utf8").trim()
    return proj || null
  } catch {
    return null
  }
}

// Project-root mode: opencode is launched with cwd = projects/<name>, so the
// workspace is the project store itself. In workspace mode the cwd is
// opencode_cc_mem and projects/<name> holds the store.
function isProjectRoot(directory: string): boolean {
  return (
    process.env.MAGNOLIA_PROJECT_WORKSPACE === "1" ||
    existsSync(join(directory, ".magnolia", "GOAL.md")) ||
    existsSync(join(directory, ".magnolia", "boot-context.md"))
  )
}

function workspaceRoot(directory: string): string {
  if (!isProjectRoot(directory)) return directory
  let cur = directory
  while (cur !== "/") {
    if (
      existsSync(join(cur, "rules")) ||
      existsSync(join(cur, "mcp-servers", "compchem-memory"))
    ) {
      return cur
    }
    cur = dirname(cur)
  }
  return directory
}

function projectName(directory: string): string | null {
  if (isProjectRoot(directory)) return basename(directory)
  return activeProject(directory)
}

function projectStore(directory: string, project: string): string {
  return isProjectRoot(directory)
    ? join(directory, ".magnolia")
    : join(directory, "projects", project, ".magnolia")
}

/**
 * The capture plugin claims a session in exactly one project mapping at its
 * first prompt. Follow that claim so boot-context and the handover agree even
 * if .active-project flips later. Fall back to the marker when the mapping
 * has not been written yet.
 */
function mappedProject(directory: string, sessionID: string): string | null {
  try {
    for (const entry of readdirSync(join(directory, "projects"), { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      const map = join(
        directory,
        "projects",
        entry.name,
        ".magnolia",
        "opencode-sessions.jsonl",
      )
      try {
        if (readFileSync(map, "utf8").includes(sessionID)) return entry.name
      } catch {
        /* project has no mapping yet */
      }
    }
  } catch {
    /* no projects directory */
  }
  return null
}

function projectPayload(directory: string, project: string): string {
  const store = projectStore(directory, project)
  const parts: string[] = [
    `<!-- magnolia-boot-context:${project} -->`,
    `[MAGNOLIA BOOT CONTEXT — project ${project}]`,
  ]

  for (const name of ["boot-context.md", "audit-report.md"]) {
    const text = readCached(join(store, name))
    if (text.trim()) parts.push(`--- ${name} ---\n${text.trim()}`)
  }

  try {
    const rulesDir = join(workspaceRoot(directory), "rules")
    for (const name of readdirSync(rulesDir).filter((f) => f.endsWith(".md")).sort()) {
      const text = readCached(join(rulesDir, name))
      if (text.trim()) parts.push(`--- rules/${name} ---\n${text.trim()}`)
    }
  } catch {
    /* no rules directory — fine */
  }

  let out = parts.join("\n\n")
  if (out.length > MAX_PAYLOAD_CHARS) {
    out = out.slice(0, MAX_PAYLOAD_CHARS) + "\n\n[truncated at 256k chars]"
  }
  return out
}

export default {
  id: "magnolia-boot-context",

  async setup(ctx: any) {
    if (!ctx?.session?.hook) return // v1 runs server() instead
    const directory = ctx.location?.directory ?? process.cwd()
    const projectBySession = new Map<string, string | null>()

    const projectFor = (sessionID: string): string | null => {
      if (!projectBySession.has(sessionID)) {
        // In project-root mode the workspace is the store; in workspace mode
        // the marker matches the memory server's current pin. Mapping is only
        // a fallback for old stores without a marker.
        projectBySession.set(
          sessionID,
          projectName(directory) ?? mappedProject(directory, sessionID),
        )
      }
      return projectBySession.get(sessionID) ?? null
    }

    await ctx.session.hook("context", async (event: any) => {
      try {
        const sessionID = String(event?.sessionID ?? "")
        if (!sessionID || !Array.isArray(event?.system)) return
        const project = projectFor(sessionID)
        if (!project) return

        const text = projectPayload(directory, project)
        if (!text) return
        const marker = `<!-- magnolia-boot-context:${project} -->`
        const already = event.system.some(
          (part: any) => typeof part?.text === "string" && part.text.includes(marker),
        )
        if (!already) event.system.push({ type: "text", text })
      } catch {
        /* never break a model request */
      }
    })
  },
}
