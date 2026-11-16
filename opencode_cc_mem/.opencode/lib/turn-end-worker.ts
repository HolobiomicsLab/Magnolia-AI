/** Synchronously hand OpenCode turn-end work to Magnolia's Python worker. */
import { spawn as nodeSpawn } from "node:child_process"
import {
  closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync,
} from "node:fs"
import { delimiter, dirname, join, resolve, basename } from "node:path"
import { fileURLToPath } from "node:url"

type Operation = "capture" | "critic" | "probe"

const MODULE_DIR = dirname(fileURLToPath(import.meta.url))
const PACKAGE_ROOT = resolve(MODULE_DIR, "../..")
const PACKAGE_SRC = join(PACKAGE_ROOT, "mcp-servers/compchem-memory/src")
const VENV_PYTHON = join(PACKAGE_ROOT, ".venv", "bin", "python3")
let sequence = 0

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
      ...paths, ...details, harness: "opencode", session_id: sessionID, operation,
    }
    writeFileSync(jobPath, JSON.stringify(payload), { encoding: "utf8", flag: "wx" })

    const logDescriptor = openSync(join(workerDir, "opencode-turn-end.log"), "a")
    try {
      const python = process.env.MAGNOLIA_PYTHON ||
        (existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3")
      const spawnProcess = (globalThis as any).__MAGNOLIA_TEST_SPAWN ?? nodeSpawn
      const child = spawnProcess(
        python,
        ["-m", "compchem_memory.hook.background", "--opencode-turn", jobPath],
        {
          cwd: directory,
          detached: true,
          stdio: ["ignore", logDescriptor, logDescriptor],
          env: {
            ...process.env,
            MAGNOLIA_ROOT: paths.root,
            MAGNOLIA_PROJECT_DIR: paths.project_dir,
            PYTHONPATH: [PACKAGE_SRC, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
          },
        },
      )
      child.unref()
      return true
    } finally {
      closeSync(logDescriptor)
    }
  } catch {
    // A written payload is a successful handoff even if this spawn failed:
    // the next turn's worker drains stale payloads from the same session.
    return !!jobPath && existsSync(jobPath)
  }
}
