/** Synchronously hand OpenCode turn-end work to Magnolia's Python worker. */
import { spawn as nodeSpawn } from "node:child_process"
import {
  closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync,
} from "node:fs"
import { join, resolve, basename } from "node:path"
import {
  isolatedModuleArgs, PACKAGE_ROOT, resolveMagnoliaPython,
} from "./magnolia-python.ts"

type Operation = "capture" | "critic" | "probe"

let sequence = 0

const WORKER_ENV_KEYS = [
  "HOME", "PATH", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
  "http_proxy", "https_proxy", "all_proxy", "no_proxy", "OPENCODE_DB",
  "MAGNOLIA_CRITIC", "MAGNOLIA_CRITIC_PROVIDER", "MAGNOLIA_CRITIC_MODEL",
  "MAGNOLIA_CRITIC_API_KEY", "MAGNOLIA_CRITIC_BASE_URL",
  "MAGNOLIA_OPENROUTER_PROVIDER", "MAGNOLIA_OPENROUTER_STICKY",
  "OPENROUTER_REFERER", "OPENROUTER_TITLE", "MAGNOLIA_LLM_API_KEY",
  "MAGNOLIA_OPENAI_API_KEY", "MAGNOLIA_MISTRAL_API_KEY",
  "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
  "KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY",
  "MISTRAL_API_KEY", "ZHIPU_API_KEY", "ZAI_API_KEY",
  "DEEPSEEK_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
  "KIMI_BASE_URL", "OPENROUTER_BASE_URL", "MISTRAL_BASE_URL", "ZAI_BASE_URL",
] as const

function isProjectRoot(directory: string): boolean {
  return process.env.MAGNOLIA_PROJECT_WORKSPACE === "1" ||
    existsSync(join(directory, ".magnolia", "GOAL.md")) ||
    existsSync(join(directory, ".magnolia", "boot-context.md"))
}

function activeProject(directory: string): string | null {
  try {
    const name = readFileSync(join(directory, ".magnolia", ".active-project"), "utf8").trim()
    return name ? join(directory, "projects", name) : null
  } catch { return null }
}

function projectPaths(directory: string) {
  const rootMode = isProjectRoot(directory)
  const configured = process.env.MAGNOLIA_PROJECT_DIR
  const projectDir = rootMode
    ? directory
    : activeProject(directory) ?? (configured
      ? (configured.startsWith("/") ? configured : join(directory, configured))
      : directory)
  return {
    root: PACKAGE_ROOT,
    project_dir: resolve(projectDir),
    store: join(resolve(projectDir), ".magnolia"),
    name: basename(projectDir),
    root_mode: rootMode,
  }
}

/** Return the user-message boundary represented by a terminal event. */
export function eventTurnID(event: any): string | null {
  const raw = event?.turnID ?? event?.turn_id ?? event?.data?.turnID ??
    event?.data?.turn_id ?? event?.data?.messageID ?? event?.properties?.turnID ??
    event?.properties?.turn_id ?? event?.properties?.messageID ?? event?.messageID
  return raw === undefined || raw === null || raw === "" ? null : String(raw)
}

/** Preserve only event identity fields OpenCode actually emitted. */
export function eventTurnDetails(event: any): Record<string, unknown> {
  const eventID = event?.id
  const durableSeq = event?.durable?.seq
  return {
    turn_id: eventTurnID(event),
    event_id: eventID === undefined || eventID === null || eventID === ""
      ? null
      : String(eventID),
    durable_seq: durableSeq === undefined || durableSeq === null || durableSeq === ""
      ? null
      : durableSeq,
  }
}

function workerEnvironment(paths: ReturnType<typeof projectPaths>): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {}
  for (const key of WORKER_ENV_KEYS) {
    if (process.env[key] !== undefined) environment[key] = process.env[key]
  }
  environment.MAGNOLIA_ROOT = paths.root
  environment.MAGNOLIA_PROJECT_DIR = paths.project_dir
  return environment
}

/** Write the durable payload and detach before returning to the event loop. */
export function handoffTurnEnd(
  directory: string,
  sessionID: string,
  operation: Operation,
  details: Record<string, unknown> = {},
): boolean {
  let jobPath = ""
  try {
    const paths = projectPaths(directory)
    const workerDir = join(paths.store, "hook-workers")
    mkdirSync(workerDir, { recursive: true })
    const suffix = `${process.pid}-${Date.now()}-${sequence++}`
    jobPath = join(workerDir, `opencode-turn-${suffix}.json`)
    const payload = {
      ...paths, ...details, queued_at: Date.now() / 1000,
      harness: "opencode", session_id: sessionID, operation,
    }
    writeFileSync(jobPath, JSON.stringify(payload), { encoding: "utf8", flag: "wx" })

    const logDescriptor = openSync(join(workerDir, "opencode-turn-end.log"), "a")
    try {
      const spawnProcess = (globalThis as any).__MAGNOLIA_TEST_SPAWN ?? nodeSpawn
      const child = spawnProcess(
        resolveMagnoliaPython(),
        isolatedModuleArgs("compchem_memory.hook.background", ["--opencode-turn", jobPath]),
        {
          cwd: PACKAGE_ROOT,
          detached: true,
          stdio: ["ignore", logDescriptor, logDescriptor],
          env: workerEnvironment(paths),
        },
      )
      child.unref()
      return true
    } finally {
      closeSync(logDescriptor)
    }
  } catch {
    // A written payload is a successful handoff even if this spawn failed:
    // any later project worker or startup sweep drains pending payloads.
    return !!jobPath && existsSync(jobPath)
  }
}
