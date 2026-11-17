/**
 * magnolia-session-capture — PROTOTYPE
 *
 * Captures the authoritative opencode conversation session id at its source and
 * records the magnolia<->opencode mapping. This is the one place the session id
 * is known reliably (the MCP servers only get OPENCODE_RUN_ID/PID, never ses_<id>).
 *
 * On each user message it appends, once per new session id, a line to:
 *   <directory>/.magnolia/opencode-sessions.jsonl
 *     {"ts","opencode_session_id","directory","worktree","title?"}
 *
 * Downstream (Python) ingestion reads that file and, for each not-yet-distilled
 * session id, runs the export CLI to feed the REAL transcript into distillation
 * (`opencode export <id> --sanitize` on v1; `opencode session export <id>
 * --sanitize` on v2 — and, once the in-process capture lands, the plugin-side
 * transcript writer below). See
 * docs/superpowers/specs/2026-06-03-opencode-conversation-ingestion-design.md
 *
 * Defensive by design: any failure is swallowed so a capture problem can never
 * break the opencode session.
 *
 * DUAL-SHAPE (2026-10-07, probe-verified on 1.18.34 + 2.0.6): plain-object
 * default export. v1 server() keeps the original chat.message + event hooks;
 * v2 setup(ctx) uses the prompt-admission hook (carries sessionID readonly)
 * plus an event-stream subscription as belt-and-suspenders — both feeding the
 * same recorder, so the JSONL contract the Python side reads is unchanged.
 *
 * OpenCode 2.3.0 (probed 2026-11-14): loads. The prompt hook writes the
 * session mapping; turn end synchronously detaches Python capture finalisation
 * because `opencode run` does not await event-started async work.
 */
import { appendFileSync, mkdirSync, existsSync, readFileSync } from "node:fs"
import { join, dirname, basename } from "node:path"
import { eventTurnDetails, handoffTurnEnd } from "../lib/turn-end-worker.ts"

function isProjectRoot(directory: string): boolean {
  return (
    process.env.MAGNOLIA_PROJECT_WORKSPACE === "1" ||
    existsSync(join(directory, ".magnolia", "GOAL.md")) ||
    existsSync(join(directory, ".magnolia", "boot-context.md"))
  )
}

function activeProject(directory: string): string | null {
  // magnolia writes the active project name to .magnolia/.active-project
  // before exec-ing opencode, because opencode does not pass shell env vars
  // to plugin code. This marker file is the only reliable channel.
  if (isProjectRoot(directory)) return basename(directory)
  try {
    const proj = readFileSync(join(directory, ".magnolia", ".active-project"), "utf8").trim()
    return proj || null
  } catch { return null }
}

function projectStore(directory: string, project: string): string {
  return isProjectRoot(directory)
    ? join(directory, ".magnolia")
    : join(directory, "projects", project, ".magnolia")
}

function readSeen(path: string): Set<string> {
  const seen = new Set<string>()
  try {
    if (!existsSync(path)) return seen
    for (const line of readFileSync(path, "utf8").split("\n")) {
      if (!line.trim()) continue
      try { seen.add(JSON.parse(line).opencode_session_id) } catch { /* skip */ }
    }
  } catch { /* first run / unreadable */ }
  return seen
}

type Recorder = ((sessionID: string) => void) & {
  isKnown: (sessionID: string) => boolean
  projectOf: (sessionID: string) => string | null
}

function makeRecorder(directory: string, worktree: string | undefined): Recorder {
  // Shared root mapping: migration buffer only. It must NOT be the dedup set
  // for project mappings — that made the first plugin instance claim every
  // session on the shared v2 service (2026-11-10 cross-project mapping mix).
  const rootPath = join(directory, ".magnolia", "opencode-sessions.jsonl")
  const seenRoot = readSeen(rootPath)

  // Bind a session to the project active at its FIRST event and keep it there.
  // A later .active-project flip must not move an existing session.
  const projectBySession = new Map<string, string | null>()
  const seenByProject = new Map<string, Set<string>>()

  const projectFor = (sessionID: string): string | null => {
    if (!projectBySession.has(sessionID)) {
      projectBySession.set(sessionID, activeProject(directory))
    }
    return projectBySession.get(sessionID) ?? null
  }

  const projectSeen = (path: string): Set<string> => {
    let seen = seenByProject.get(path)
    if (!seen) {
      seen = readSeen(path)
      seenByProject.set(path, seen)
    }
    return seen
  }

  const recordOne = (path: string, sessionID: string) => {
    try {
      mkdirSync(dirname(path), { recursive: true })
      const entry = {
        ts: new Date().toISOString(),
        opencode_session_id: sessionID,
        directory,
        worktree,
      }
      appendFileSync(path, JSON.stringify(entry) + "\n")
    } catch { /* never throw into opencode */ }
  }

  const record = ((sessionID: string) => {
    if (!sessionID) return
    const proj = projectFor(sessionID)
    if (!proj) return
    const projPath = join(
      projectStore(directory, proj),
      "opencode-sessions.jsonl",
    )
    // Dedup per project mapping. The root is written separately so two
    // plugin instances cannot make the first project own every session.
    const seen = projectSeen(projPath)
    if (!seen.has(sessionID)) {
      recordOne(projPath, sessionID)
      seen.add(sessionID)
    }
    // In project-root mode the project mapping IS the root; write once.
    if (projPath !== rootPath && !seenRoot.has(sessionID)) {
      recordOne(rootPath, sessionID)
      seenRoot.add(sessionID)
    }
  }) as Recorder

  record.isKnown = (sessionID: string) => projectBySession.has(sessionID)
  record.projectOf = (sessionID: string) => projectBySession.get(sessionID) ?? null
  return record
}

export default {
  id: "magnolia-session-capture",

  /** opencode v2: prompt hook (sessionID readonly on the event) + event stream. */
  async setup(ctx: any) {
    if (!ctx?.session?.hook) return // v1.18.34 also calls setup() with a limited ctx — v1 runs server()
    const directory = ctx.location?.directory ?? process.cwd()
    const record = makeRecorder(directory, (ctx as any)?.location?.workspaceID ?? undefined)

    await ctx.session.hook("prompt", async (event: any) => {
      try { record(event?.sessionID) } catch { /* never throw */ }
    })

    // Belt-and-suspenders: lifecycle events also carry the id. At turn end the
    // detached worker reads the durable v2 database and writes the distill feed.
    // v2 event vocabulary (logged 2026-10-07): there is NO session.idle in the
    // stream; the turn-terminal signal is session.execution.succeeded with the
    // session id at durable.aggregateID (data.sessionID on usage events).
    // The 2.3.0 binary defines both session.idle and session.execution.*;
    // which one this stream delivers is not established (2026-11-14).
    // Accept both vocabularies.
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const ev: any = event
          const sid =
            ev?.durable?.aggregateID ??
            ev?.properties?.sessionID ??
            ev?.properties?.info?.id ??
            ev?.data?.sessionID ??
            ev?.sessionID
          if (!sid) continue
          const sidStr = String(sid)
          // The service event stream is global. Only sessions this plugin saw
          // through the location-scoped prompt hook belong to this workspace's
          // project; skip API/test sessions and other locations entirely.
          if (!record.isKnown(sidStr)) continue
          record(sidStr)
          if (!record.projectOf(sidStr)) continue
          const turnEnd =
            ev?.type === "session.idle" || ev?.type === "session.execution.succeeded"
          if (turnEnd) {
            handoffTurnEnd(
              directory, sidStr, "capture", eventTurnDetails(ev),
              record.projectOf(sidStr) ?? undefined,
            )
          }
        }
      } catch { /* aborted or stream error — never break the session */ }
    })()
    return () => controller.abort()
  },

  /** opencode v1: original chat.message + event hooks. */
  async server({ directory, worktree }: any) {
    const record = makeRecorder(directory, worktree)
    return {
      // Primary: fires per user message with the session id.
      "chat.message": async (input: any) => {
        record(input?.sessionID)
      },
      // Belt-and-suspenders: session lifecycle bus events also carry the id,
      // in case chat.message's shape differs across opencode versions.
      event: (input: any) => {
        const sid =
          input?.event?.properties?.sessionID ??
          input?.event?.properties?.info?.id ??
          input?.event?.sessionID
        if (sid) {
          record(sid)
          if (input?.event?.type === "session.idle") {
            handoffTurnEnd(
              directory, String(sid), "capture", eventTurnDetails(input?.event),
              record.projectOf(String(sid)) ?? undefined,
            )
          }
        }
      },
    }
  },
}
