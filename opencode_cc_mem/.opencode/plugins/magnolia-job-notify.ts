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
 */
import { readFileSync, unlinkSync, writeFileSync } from "node:fs"
import { join } from "node:path"

const ENABLED = process.env.MAGNOLIA_JOB_NOTIFY !== "0"
const POLL_INTERVAL_MS = 30_000
const MAX_PER_TICK = 10 // cap toasts per tick; the rest wait for the next one

type Toast = (title: string, message: string, variant?: string) => void

function startPolling(noticesPath: string, toast: Toast): () => void {
  const ICONS: Record<string, string> = {
    success: "✅",
    science_failure: "❌",
    infra_failure: "⚠️",
  }

  function poll(): void {
    let lines: string[]
    try {
      lines = readFileSync(noticesPath, "utf-8").split("\n")
    } catch {
      return // absent (normal steady state) or unreadable — nothing to do
    }
    const taken: any[] = []
    const kept: string[] = []
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue
      if (taken.length >= MAX_PER_TICK) {
        kept.push(trimmed)
        continue
      }
      try {
        const rec = JSON.parse(trimmed)
        if (rec && typeof rec === "object") taken.push(rec)
      } catch {
        /* malformed line: drop rather than stall the queue */
      }
    }
    if (taken.length === 0) {
      // Nothing consumable: only touch the file if it held only malformed
      // lines (clean them up); otherwise leave it untouched.
      if (lines.some((l) => l.trim()) && kept.length === 0) {
        try {
          unlinkSync(noticesPath)
        } catch {}
      }
      return
    }
    try {
      if (kept.length > 0) writeFileSync(noticesPath, kept.join("\n") + "\n")
      else unlinkSync(noticesPath)
    } catch {}
    for (const rec of taken) {
      const icon = ICONS[String(rec.category)] ?? "🔔"
      const tool = String(rec.tool ?? "job")
      const state = String(rec.state ?? "")
      const runId = String(rec.run_id ?? "")
      const tail = runId.length > 24 ? runId.slice(-24) : runId
      const message = `${icon} ${tool} finished (${state}) — run …${tail}`.slice(0, 160)
      toast("job-finished", message, rec.category === "success" ? undefined : "warning")
    }
  }

  poll() // catch notices that piled up before this session started
  const timer = setInterval(poll, POLL_INTERVAL_MS)
  // Do not keep the process alive just for the timer.
  ;(timer as any)?.unref?.()
  return () => clearInterval(timer)
}

function noticesFile(directory: string): string {
  // MAGNOLIA_PROJECT_DIR is exported by the magnolia wrapper (e.g.
  // "projects/xiulian"); fall back to the opencode root directory if unset.
  const projectDir = process.env.MAGNOLIA_PROJECT_DIR
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
