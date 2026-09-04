/**
 * magnolia-action-retrieval — action-time memory retrieval (v1, soft injection)
 *
 * Problem (2026-06-24 diagnosis, staging entry "Session-start retrieval misses
 * the action moment"): both session-start architectures fire when there is no
 * concrete action yet, so the query is vague and the agent never checks learned
 * knowledge BEFORE costly/mutating actions (submit_job, mutating run_shell,
 * write/edit). opencode 1.18.25 exposes the missing hook surface:
 *
 *   tool.execute.before  { tool, sessionID, callID } -> { args }   (mutable)
 *   tool.execute.after   { tool, sessionID, callID, args } -> { title, output, metadata }
 *
 * Design: on `before` for an instrumented tool, build a query from the tool
 * name + string args and kick off a lean python search over the memory tiers
 * (compchem_memory.quick_search — tiers-only import, ~100 ms, no MCP SDK).
 * On `after`, prepend the ranked hits as a clearly-marked preamble to the tool
 * RESULT (soft injection — the agent sees the knowledge at the moment the
 * action's outcome is produced). No gating, no blocking in v1.
 *
 * Noise control: only hits with score >= 2 (distinct query tokens matched) or
 * confidence >= 0.8 are injected, capped at 4. Identical queries within 120 s
 * reuse the cached result. The retrieval child is killed at 2500 ms; on any
 * failure the tool result passes through untouched.
 *
 * Safe by design: retrieval is READ-ONLY and every failure is swallowed —
 * this plugin must never break a tool call.
 *
 * Toggle: MAGNOLIA_ACTION_RETRIEVE=0|off|false|no disables it.
 * Log: <project>/.magnolia/action-retrieval.jsonl (one line per injection).
 */
import { execFile } from "node:child_process"
import { appendFileSync, mkdirSync, readFileSync } from "node:fs"
import { join, dirname } from "node:path"
import { promisify } from "node:util"

import type { Plugin } from "@opencode-ai/plugin"

const execFileP = promisify(execFile)

const DISABLED = ["0", "off", "false", "no"].includes(
  String(process.env.MAGNOLIA_ACTION_RETRIEVE ?? "").toLowerCase(),
)

const PYTHON = process.env.MAGNOLIA_PYTHON || "/home/user/repos/project_magnolia/.venv/bin/python3"
const QUERY_TIMEOUT_MS = 2500
const CACHE_TTL_MS = 120_000
const MAX_HITS = 4

// Tools whose invocation is an "action" worth checking memory for first:
// state-changing shell, submissions, file writes/edits, and the main
// scientific executors. Add tools here as they prove noisy to omit.
const ACTION_TOOLS = new Set([
  "submit_job",
  "compchem-tools_run_shell",
  "compchem-tools_submit_job",
  "write",
  "edit",
  "compchem-tools_gnina_dock",
  "compchem-tools_gromacs_run",
  "compchem-tools_gromacs_setup",
  "compchem-tools_haddock3_run",
  "compchem-tools_gaussian_run",
  "compchem-tools_orca_run",
  "compchem-tools_xtb_optimize",
  "compchem-tools_xtb_singlepoint",
  "compchem-tools_preprocess_pdb",
  "compchem-tools_run_acpype",
])

function shortTool(tool: string): string {
  return tool.replace(/^compchem-tools_/, "")
}

/** Collect string leaf values from tool args (depth <= 2) as query text. */
function argsToQuery(args: any): string {
  const parts: string[] = []
  const walk = (v: any, depth: number) => {
    if (v == null) return
    if (typeof v === "string") {
      if (v.length > 0 && v.length < 2000) parts.push(v)
    } else if (depth < 2) {
      if (Array.isArray(v)) v.forEach((x) => walk(x, depth + 1))
      else if (typeof v === "object") Object.values(v).forEach((x) => walk(x, depth + 1))
    }
  }
  walk(args, 0)
  return parts.join(" ").slice(0, 400)
}

export const MagnoliaActionRetrieval: Plugin = async ({ directory }) => {
  if (DISABLED) return {}

  // Resolve the active project the same way magnolia-auto-retrieval does
  // (.magnolia/.active-project at the plugin directory root).
  let projectDir = ""
  try {
    const proj = readFileSync(join(directory, ".magnolia", ".active-project"), "utf8").trim()
    if (proj) projectDir = join("projects", proj)
  } catch { /* fall through: MAGNOLIA_PROJECT_DIR env decides in python */ }

  const childEnv = {
    ...process.env,
    MAGNOLIA_ROOT: directory,
    MAGNOLIA_PROJECT_DIR: projectDir || process.env.MAGNOLIA_PROJECT_DIR || "projects/xiulian",
    PYTHONPATH: join(directory, "mcp-servers/compchem-memory/src"),
  }

  const logFile = projectDir
    ? join(directory, projectDir, ".magnolia", "action-retrieval.jsonl")
    : join(directory, ".magnolia", "action-retrieval.jsonl")

  const log = (row: Record<string, unknown>) => {
    try {
      mkdirSync(dirname(logFile), { recursive: true })
      appendFileSync(logFile, JSON.stringify({ ts: new Date().toISOString(), ...row }) + "\n")
    } catch { /* never throw into opencode */ }
  }

  // callID -> in-flight retrieval promise
  const pending = new Map<string, Promise<{ lines: string[]; ms: number; query: string } | null>>()
  // query -> { ts, lines } result cache
  const cache = new Map<string, { ts: number; lines: string[] }>()

  async function retrieve(query: string): Promise<{ lines: string[]; ms: number } | null> {
    if (!query.trim()) return null
    const hit = cache.get(query)
    if (hit && Date.now() - hit.ts < CACHE_TTL_MS) return { lines: hit.lines, ms: 0 }
    const t0 = Date.now()
    try {
      const { stdout } = await execFileP(
        PYTHON,
        ["-m", "compchem_memory.quick_search", query, "--k", String(MAX_HITS)],
        { cwd: directory, env: childEnv, timeout: QUERY_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024 },
      )
      const lines = stdout.split("\n").filter((l) => l.startsWith("{"))
      if (hit) cache.delete(query) // stale entry replace below
      cache.set(query, { ts: Date.now(), lines })
      if (cache.size > 50) {
        const oldest = cache.keys().next().value
        if (oldest) cache.delete(oldest)
      }
      return { lines, ms: Date.now() - t0 }
    } catch {
      return null
    }
  }

  return {
    "tool.execute.before": async (input: any, output: any) => {
      try {
        const tool = String(input?.tool ?? "")
        if (!ACTION_TOOLS.has(tool) || !input?.callID) return
        if (pending.size > 100) {
          const oldest = pending.keys().next().value
          if (oldest) pending.delete(oldest)
        }
        pending.set(
          input.callID,
          retrieve(shortTool(tool) + " " + argsToQuery(output?.args)),
        )
      } catch { /* never throw into opencode */ }
    },

    "tool.execute.after": async (input: any, output: any) => {
      try {
        const callID = input?.callID
        const prom = pending.get(callID)
        pending.delete(callID)
        if (!prom || typeof output?.output !== "string") return
        const res = await prom
        if (!res || res.lines.length === 0) return

        const hits = res.lines
          .map((l) => { try { return JSON.parse(l) } catch { return null } })
          .filter((h): h is any => !!h)
          .filter((h) => (h.score ?? 0) >= 2 || (h.confidence ?? 0) >= 0.8)
          .slice(0, MAX_HITS)
        if (hits.length === 0) return

        const tool = String(input?.tool ?? "")
        const bullet = (h: any) => {
          const badge = [h.tier, h.type].filter(Boolean).join("/")
          const prov = h.provisional ? " [unconfirmed]" : ""
          const gist = String(h.gist ?? "").slice(0, 200)
          return `- [${badge}${prov}] ${h.title} — ${gist}\n  ${h.path}`
        }
        const block =
          `\n[Magnolia · action-memory] ${hits.length} past learning(s) matched this ` +
          `action (tool: ${shortTool(tool)}). Scan these BEFORE trusting defaults — ` +
          `one may hold the exact pitfall or parameter for this step:\n` +
          hits.map(bullet).join("\n") +
          `\n(read-only auto-injection; opt out MAGNOLIA_ACTION_RETRIEVE=0)\n\n`

        output.output = block + output.output
        log({
          sessionID: input?.sessionID,
          tool,
          callID,
          ms: res.ms,
          hits: hits.length,
          titles: hits.map((h) => h.title).slice(0, 4),
        })
      } catch { /* never throw into opencode */ }
    },
  }
}

export default MagnoliaActionRetrieval
