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
 * Noise control (v2, 2026-09-18): only hits with query-match score >= 3 are
 * injected — confidence alone must never substitute for relevance (it injected
 * two June entries on 209 of 258 staging injections in 3 weeks) — capped at 4,
 * at most 8 injection events per session, and the same entry at most twice per
 * session. Every instrumented call (injected or not) is logged with a coarse
 * outcome marker so retrieval value becomes measurable. Identical queries
 * within 120 s reuse the cached result. The retrieval child is killed at
 * 2500 ms; on any failure the tool result passes through untouched.
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

import { existsSync } from "fs"
import { join, resolve } from "path"

// Self-locate the repo venv (<repo>/.opencode/plugins/ → repo root → .venv);
// no machine-specific absolute paths in the repo. MAGNOLIA_PYTHON overrides.
const REPO_ROOT = resolve(import.meta.dir, "../../..")
const VENV_PY = join(REPO_ROOT, ".venv", "bin", "python3")
const PYTHON = process.env.MAGNOLIA_PYTHON || (existsSync(VENV_PY) ? VENV_PY : "python3")
const QUERY_TIMEOUT_MS = 2500
const CACHE_TTL_MS = 120_000
const MAX_HITS = 4
const MIN_SCORE = 3
const MAX_INJECTIONS_PER_SESSION = 8
const MAX_PER_ENTRY_PER_SESSION = 2

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

  // C1 noise-control state, keyed by session (bounded: oldest dropped at 40)
  const sessionInjections = new Map<string, number>()
  const entryLatch = new Map<string, Map<string, number>>()
  const skipLogged = new Map<string, Set<string>>()
  const touchSession = (sid: string) => {
    if (!sid || sessionInjections.has(sid)) return
    if (sessionInjections.size >= 40) {
      for (const k of [...sessionInjections.keys()].slice(0, sessionInjections.size - 20)) {
        sessionInjections.delete(k)
        entryLatch.delete(k)
        skipLogged.delete(k)
      }
    }
    sessionInjections.set(sid, 0)
  }
  const logSkip = (sid: string, tool: string, callID: unknown, reason: string) => {
    const seen = skipLogged.get(sid) ?? new Set<string>()
    skipLogged.set(sid, seen)
    if (!seen.has(reason)) {
      seen.add(reason)
      log({ sessionID: sid, tool, callID, skipped: reason })
    }
  }

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
        const sessionID = String(input?.sessionID ?? "")
        const tool = String(input?.tool ?? "")
        touchSession(sessionID)
        const prom = pending.get(callID)
        pending.delete(callID)
        if (!prom || typeof output?.output !== "string") return
        const res = await prom

        // D1 coarse outcome marker (heuristic: keyword scan of the result head;
        // a precise application link needs the callID join against the session log).
        const outText = output.output
        const outcome = /\b(error|failed|traceback|exception)\b/i.test(outText.slice(0, 400))
          ? "error?"
          : "ok"
        const logCall = (injected: number) =>
          log({ sessionID, tool, callID, outcome, injected })

        if (!res || res.lines.length === 0) { logCall(0); return }

        // C1: relevance floor — query-match score only; confidence must never
        // substitute for relevance.
        const all = res.lines
          .map((l) => { try { return JSON.parse(l) } catch { return null } })
          .filter((h): h is any => !!h && typeof h.path === "string")
          .filter((h) => (h.score ?? 0) >= MIN_SCORE)

        // C1: per-entry latch — the same entry at most twice per session.
        const latch = entryLatch.get(sessionID) ?? new Map<string, number>()
        entryLatch.set(sessionID, latch)
        const fresh = all.filter((h) => (latch.get(h.path) ?? 0) < MAX_PER_ENTRY_PER_SESSION)

        // C1: per-session budget.
        const used = sessionInjections.get(sessionID) ?? 0
        if (fresh.length === 0) { logSkip(sessionID, tool, callID, "latch"); logCall(0); return }
        if (used >= MAX_INJECTIONS_PER_SESSION) {
          logSkip(sessionID, tool, callID, "budget")
          logCall(0)
          return
        }

        const hits = fresh.slice(0, MAX_HITS)
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
        sessionInjections.set(sessionID, used + 1)
        for (const h of hits) latch.set(h.path, (latch.get(h.path) ?? 0) + 1)
        logCall(hits.length)
        log({
          sessionID,
          tool,
          callID,
          ms: res.ms,
          hits: hits.length,
          titles: hits.map((h) => h.title).slice(0, 4),
          entries: hits.map((h) => ({
            path: h.path,
            tier: h.tier,
            score: h.score,
            confidence: h.confidence,
            provisional: !!h.provisional,
          })),
        })
      } catch { /* never throw into opencode */ }
    },
  }
}

export default MagnoliaActionRetrieval
