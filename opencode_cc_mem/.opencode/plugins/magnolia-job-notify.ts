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
 */
import { readFileSync, unlinkSync, writeFileSync } from "node:fs"
import { join } from "node:path"

import type { Plugin } from "@opencode-ai/plugin"

const ENABLED = process.env.MAGNOLIA_JOB_NOTIFY !== "0"
const POLL_INTERVAL_MS = 30_000
const MAX_PER_TICK = 10 // cap toasts per tick; the rest wait for the next one

export const JobNotify: Plugin = async ({ client, directory }) => {
  if (!ENABLED) return {}

  // MAGNOLIA_PROJECT_DIR is exported by the magnolia wrapper (e.g.
  // "projects/xiulian"); fall back to the opencode root directory if unset.
  const projectDir = process.env.MAGNOLIA_PROJECT_DIR
  const noticesPath = projectDir
    ? join(directory, projectDir, ".magnolia", ".job-notices.jsonl")
    : join(directory, ".magnolia", ".job-notices.jsonl")

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
      const variant = rec.category === "success" ? undefined : "warning"
      client.tui
        .showToast({
          body: variant
            ? { title: "job-finished", message, variant }
            : { title: "job-finished", message },
        })
        .catch(() => {})
    }
  }

  poll() // catch notices that piled up before this session started
  const timer = setInterval(poll, POLL_INTERVAL_MS)
  // Do not keep the process alive just for the timer.
  ;(timer as any)?.unref?.()
  return {}
}
