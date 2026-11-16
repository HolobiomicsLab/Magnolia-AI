/**
 * magnolia-job-notify — delivers finished-job notices without losing headless runs.
 *
 * OpenCode v2 receives notices through its context hook, which works in both
 * the TUI and `opencode run`. OpenCode v1 keeps its existing toast behavior.
 * A notice is claimed before delivery and removed only after the context or
 * toast API accepts it. A claim is a Python child that holds the project's
 * job-notice lock (the one the hook core uses) from peek until this plugin
 * writes "commit" or "release" to its stdin, so hook clients and OpenCode
 * cannot drain the same project notice concurrently. If this process dies the
 * child sees EOF and releases without consuming.
 *
 * The bridge runs Magnolia's configured Python with -I (isolated import
 * paths), and a queue holding only malformed lines never starts Python: those
 * rows belong to the hook core's cleanup path.
 *
 * No-op when MAGNOLIA_JOB_NOTIFY=0, the queue is absent, no delivery channel
 * exists, or Magnolia's configured Python cannot take the shared lock.
 * Failures never break a session.
 */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const ENABLED = process.env.MAGNOLIA_JOB_NOTIFY !== "0"
const POLL_INTERVAL_MS = 30_000
const MAX_PER_DELIVERY = 10
const CLAIM_TIMEOUT_MS = 5_000

// Self-locate the Magnolia root and its venv. MAGNOLIA_PYTHON is the portable
// override for tests and nonstandard installations.
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..")
const VENV_PY = join(REPO_ROOT, ".venv", "bin", "python3")
const PYTHON = process.env.MAGNOLIA_PYTHON || (existsSync(VENV_PY) ? VENV_PY : null)
const SOURCE_DIR = join(REPO_ROOT, "opencode_cc_mem", "mcp-servers", "compchem-memory", "src")
// -I ignores PYTHONPATH and the working directory, so a project file cannot
// shadow a standard module; the Magnolia source dir is added explicitly.
const BRIDGE = [
  "import sys",
  "sys.path.insert(0, sys.argv[1])",
  "sys.argv[1:] = sys.argv[2:]",
  "from compchem_memory.job_notices import _main",
  "raise SystemExit(_main())",
].join("; ")

type Notice = Record<string, unknown>
type Claim = { child: ChildProcessWithoutNullStreams; rows: Notice[]; closed: Promise<void> }
type Toast = (title: string, message: string, variant?: string) => void | Promise<void>

function claimNotices(noticesPath: string, limit: number): Promise<Claim | null> {
  // Malformed rows belong to the hook core's cleanup path; never start Python for them.
  if (!PYTHON || !existsSync(noticesPath) || !hasValidNotice(noticesPath)) return Promise.resolve(null)
  const projectDir = dirname(dirname(noticesPath))
  return new Promise((resolveClaim) => {
    const child = spawn(
      PYTHON,
      ["-I", "-c", BRIDGE, SOURCE_DIR, "--claim", projectDir, "--limit", String(limit)],
      { env: process.env },
    )
    let buffer = ""
    let answered = false
    const closed = new Promise<void>((done) => child.on("close", () => done()))
    const answer = (claim: Claim | null) => {
      if (answered) return
      answered = true
      clearTimeout(timer)
      if (!claim) child.kill()
      resolveClaim(claim)
    }
    const timer = setTimeout(() => answer(null), CLAIM_TIMEOUT_MS)
    child.stdin.on("error", () => {})
    child.stderr.resume()
    child.on("error", () => answer(null))
    child.on("close", () => answer(null))
    child.stdout.setEncoding("utf8")
    child.stdout.on("data", (chunk) => {
      buffer += chunk
      const newline = buffer.indexOf("\n")
      if (newline < 0) return
      try {
        const rows = JSON.parse(buffer.slice(0, newline))
        answer(Array.isArray(rows) && rows.length > 0 ? { child, rows, closed } : null)
      } catch {
        answer(null)
      }
    })
  })
}

function hasValidNotice(noticesPath: string): boolean {
  try {
    return readFileSync(noticesPath, "utf8").split(/\r?\n/).some((line) => {
      try {
        const value = JSON.parse(line)
        return value !== null && typeof value === "object" && !Array.isArray(value)
      } catch {
        return false
      }
    })
  } catch {
    return false
  }
}

async function settleClaim(claim: Claim, delivered: boolean): Promise<void> {
  try {
    claim.child.stdin.write(delivered ? "commit\n" : "release\n")
    claim.child.stdin.end()
  } catch {
    claim.child.kill()
  }
  await claim.closed
}

function noticeLine(rec: Notice): string {
  const icons: Record<string, string> = {
    success: "✅",
    science_failure: "❌",
    infra_failure: "⚠️",
  }
  const category = String(rec.category ?? "")
  const icon = icons[category] ?? "🔔"
  const tool = String(rec.tool ?? "job")
  const state = String(rec.state ?? "")
  const runId = String(rec.run_id ?? "")
  const tail = runId.length > 24 ? runId.slice(-24) : runId
  return `${icon} ${tool} finished (${state}) — run …${tail}`.slice(0, 160)
}

export function startPolling(
  noticesPath: string,
  toast: Toast,
  pollIntervalMs = POLL_INTERVAL_MS,
): () => void {
  let isPolling = false
  async function poll(): Promise<void> {
    if (isPolling) return
    isPolling = true
    try {
      for (let count = 0; count < MAX_PER_DELIVERY; count += 1) {
        const claim = await claimNotices(noticesPath, 1)
        if (!claim) break
        const rec = claim.rows[0]
        try {
          await toast(
            "job-finished",
            noticeLine(rec),
            rec.category === "success" ? undefined : "warning",
          )
          await settleClaim(claim, true)
        } catch {
          await settleClaim(claim, false)
          break
        }
      }
    } finally {
      isPolling = false
    }
  }
  void poll()
  const timer = setInterval(() => void poll(), pollIntervalMs)
  ;(timer as any)?.unref?.()
  return () => clearInterval(timer)
}

function noticesFile(directory: string): string {
  if (
    process.env.MAGNOLIA_PROJECT_WORKSPACE === "1" ||
    existsSync(join(directory, ".magnolia", "GOAL.md")) ||
    existsSync(join(directory, ".magnolia", "boot-context.md"))
  ) {
    return join(directory, ".magnolia", ".job-notices.jsonl")
  }
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

  /** OpenCode v2: deliver into model context, including headless runs. */
  async setup(ctx: any) {
    if (!ENABLED || !ctx?.session?.hook) return
    const path = noticesFile(ctx.location?.directory ?? process.cwd())
    await ctx.session.hook("context", async (event: any) => {
      if (!Array.isArray(event?.system)) return
      const claim = await claimNotices(path, MAX_PER_DELIVERY)
      if (!claim) return
      try {
        const text = `[Magnolia job notices]\n${claim.rows.map(noticeLine).join("\n")}`
        event.system.push({ type: "text", text })
        await settleClaim(claim, true)
      } catch {
        await settleClaim(claim, false)
      }
    })
  },

  /** OpenCode v1: retain toast polling, committing each accepted toast. */
  async server({ client, directory }: any) {
    if (!ENABLED || typeof client?.tui?.showToast !== "function") return {}
    const toast: Toast = async (title, message, variant) => {
      await client.tui.showToast({
        body: variant ? { title, message, variant } : { title, message },
      })
    }
    startPolling(noticesFile(directory), toast)
    return {}
  },
}
