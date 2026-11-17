/**
 * magnolia-job-notify — relays finished-job notices as toasts.
 *
 * The compchem-tools poller detects the moment a job reaches a terminal state
 * (completed / science_failure / infra_failure) and appends one JSONL line to
 *   <project>/.magnolia/.job-notices.jsonl
 * (see compchem_memory/job_notices.py). Until now nothing notified the user —
 * they had to ask in chat to learn a job finished.
 *
 * This plugin polls that file every POLL_INTERVAL_MS, shows one toast per
 * notice, and drains what it consumed (unconsumed lines stay for the next
 * tick). A timer is required because no opencode events fire while the user
 * is idle — even session.idle (claim-critic's trigger) only fires after a
 * turn, not during a long idle wait for a cluster job.
 *
 * No-op when MAGNOLIA_JOB_NOTIFY=0 or the notices file is absent (the normal
 * steady state). Defensive by design: any failure is swallowed so polling can
 * never break the opencode session.
 *
 * DUAL-SHAPE (2026-10-07, probe-verified on 1.18.34 + 2.0.6): one plain
 * object default export — v2 calls setup(ctx), v1 calls server(); v1.18.34
 * also calls setup() with a limited ctx, so setup guards on ctx.tool.hook.
 * No static @opencode/plugin import (v1 cannot resolve it; Plugin.define is
 * not required by either runtime — verified by /tmp/dualshape-test probes).
 * v2 ctx has no TUI domain server-side: toasts degrade to stderr logs.
 *
 * OpenCode 2.3.0 (probed 2026-11-14): loads; setup() completed without a
 * "failed to load plugin" warning. Not yet observed acting on 2.3.0.
 */
import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const ENABLED = process.env.MAGNOLIA_JOB_NOTIFY !== "0"
const POLL_INTERVAL_MS = 30_000
const MAX_PER_TICK = 10 // cap toasts per tick; the rest wait for the next one

// Self-locate the Magnolia root and its venv. MAGNOLIA_PYTHON is the portable
// override for tests and nonstandard installations; no native Node lock module.
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..")
const VENV_PY = join(REPO_ROOT, ".venv", "bin", "python3")
const PYTHON = process.env.MAGNOLIA_PYTHON || (existsSync(VENV_PY) ? VENV_PY : "python3")
const PYTHONPATH = join(REPO_ROOT, "opencode_cc_mem", "mcp-servers", "compchem-memory", "src")

type Toast = (title: string, message: string, variant?: string) => void

function drainNotices(noticesPath: string): Promise<any[]> {
  if (!existsSync(noticesPath)) return Promise.resolve([])
  // Delegate locking and atomic replacement to the same implementation used
  // by the hook and poller. Stream stdout: spawnSync's fixed maxBuffer could
  // reject a valid large batch after the Python side had already drained it.
  const projectDir = dirname(dirname(noticesPath))
  return new Promise((resolve) => {
    const child = spawn(
      PYTHON,
      ["-m", "compchem_memory.job_notices", "--drain", projectDir, "--limit", String(MAX_PER_TICK)],
      { env: { ...process.env, PYTHONPATH } },
    )
    let stdout = ""
    child.stdout.setEncoding("utf8")
    child.stdout.on("data", (chunk) => {
      stdout += chunk
    })
    child.stderr.resume()
    child.on("error", () => resolve([]))
    child.on("close", (code) => {
      if (code !== 0) {
        resolve([])
        return
      }
      try {
        const rows = JSON.parse(stdout)
        resolve(Array.isArray(rows) ? rows : [])
      } catch {
        resolve([])
      }
    })
  })
}

export function startPolling(
  noticesPath: string,
  toast: Toast,
  pollIntervalMs = POLL_INTERVAL_MS,
): () => void {
  const ICONS: Record<string, string> = {
    success: "✅",
    science_failure: "❌",
    infra_failure: "⚠️",
  }

  let polling = false
  async function poll(): Promise<void> {
    if (polling) return // do not accumulate Python children on a slow filesystem
    polling = true
    try {
      for (const rec of await drainNotices(noticesPath)) {
        const icon = ICONS[String(rec.category)] ?? "🔔"
        const tool = String(rec.tool ?? "job")
        const state = String(rec.state ?? "")
        const runId = String(rec.run_id ?? "")
        const tail = runId.length > 24 ? runId.slice(-24) : runId
        const message = `${icon} ${tool} finished (${state}) — run …${tail}`.slice(0, 160)
        toast("job-finished", message, rec.category === "success" ? undefined : "warning")
      }
    } finally {
      polling = false
    }
  }

  void poll() // catch notices that piled up before this session started
  const timer = setInterval(() => void poll(), pollIntervalMs)
  // Do not keep the process alive for the timer.
  ;(timer as any)?.unref?.()
  return () => clearInterval(timer)
}

function noticesFile(directory: string): string {
  // Project-root mode: cwd is the project store itself.
  if (
    process.env.MAGNOLIA_PROJECT_WORKSPACE === "1" ||
    existsSync(join(directory, ".magnolia", "GOAL.md")) ||
    existsSync(join(directory, ".magnolia", "boot-context.md"))
  ) {
    return join(directory, ".magnolia", ".job-notices.jsonl")
  }
  // Workspace mode: MAGNOLIA_PROJECT_DIR may be absolute or relative.
  const projectDir = process.env.MAGNOLIA_PROJECT_DIR
  if (projectDir && projectDir.startsWith("/")) {
    return join(projectDir, ".magnolia", ".job-notices.jsonl")
  }
  return projectDir
    ? join(directory, projectDir, ".magnolia", ".job-notices.jsonl")
    : join(directory, ".magnolia", ".job-notices.jsonl")
}

export default {
  id: "magnolia-job-notify",

  /** opencode v2: setup(ctx) — timer-only, no hooks. */
  async setup(ctx: any) {
    if (!ENABLED) return
    if (!ctx?.tool?.hook) return // v1.18.34 also calls setup() with a limited ctx — v1 runs server() instead
    const toast: Toast = (title, message, variant) => {
      const t = (ctx as any)?.tui?.showToast
      if (typeof t === "function") {
        t.call((ctx as any).tui, {
          body: variant ? { title, message, variant } : { title, message },
        }).catch(() => {})
      } else {
        console.error(`[job-notify] ${title}: ${message}`)
      }
    }
    return startPolling(noticesFile(ctx.location?.directory ?? process.cwd()), toast)
  },

  /** opencode v1: server() — original behavior, unchanged. */
  async server({ client, directory }: any) {
    if (!ENABLED) return {}
    const toast: Toast = (title, message, variant) => {
      client.tui
        .showToast({
          body: variant ? { title, message, variant } : { title, message },
        })
        .catch(() => {})
    }
    startPolling(noticesFile(directory), toast)
    return {}
  },
}
