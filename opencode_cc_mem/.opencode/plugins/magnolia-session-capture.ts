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
 */
import { appendFileSync, mkdirSync, existsSync, readFileSync, writeFileSync } from "node:fs"
import { join, dirname, basename } from "node:path"

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

/** Directory for the v2 in-process transcript dumps (next to the mapping). */
function transcriptsDir(directory: string, project?: string | null): string {
  const proj = project ?? activeProject(directory)
  if (proj) return join(projectStore(directory, proj), "opencode-transcripts")
  return join(directory, ".magnolia", "opencode-transcripts")
}

const MAX_DUMP_CHARS = 4_000_000 // ~4 MB cap; larger sessions truncate the tail

/** Dump the full in-process message list (v2 exports are user-text-only). */
function dumpTranscript(dir: string, sessionID: string, messages: unknown): void {
  try {
    mkdirSync(dir, { recursive: true })
    let body = JSON.stringify({
      ts: new Date().toISOString(),
      sessionID,
      source: "plugin-v2-ctx.session.context",
      messages,
    })
    if (body.length > MAX_DUMP_CHARS) {
      // A truncated JSON string is useless to the reader — write a valid,
      // marked object instead.
      body = JSON.stringify({
        ts: new Date().toISOString(),
        sessionID,
        source: "plugin-v2-ctx.session.context",
        truncated: true,
        note: "session exceeded 4 MB; dump clipped — use the CLI export fallback",
        messages: (Array.isArray(messages) ? messages : []).slice(0, 200),
      })
    }
    writeFileSync(join(dir, `${sessionID}.json`), body)
  } catch { /* never throw into opencode */ }
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

    // Belt-and-suspenders: session lifecycle events also carry the id, AND
    // (v2) each turn-end dumps the full in-process message list — the distill
    // feed that replaces the export CLI (v2 exports are user-text-only).
    // v2 event vocabulary (logged 2026-10-07): there is NO session.idle in the
    // stream; the turn-terminal signal is session.execution.succeeded with the
    // session id at durable.aggregateID (data.sessionID on usage events).
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
          const proj = record.projectOf(sidStr)
          if (!proj) continue
          const turnEnd =
            ev?.type === "session.idle" || ev?.type === "session.execution.succeeded"
          if (turnEnd && ctx?.session?.context) {
            const messages = await ctx.session
              .context({ sessionID: sidStr })
              .catch(() => null)
            if (messages) {
              dumpTranscript(transcriptsDir(directory, proj), sidStr, messages)
            }
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
      event: async (input: any) => {
        const sid =
          input?.event?.properties?.sessionID ??
          input?.event?.properties?.info?.id ??
          input?.event?.sessionID
        if (sid) record(sid)
      },
    }
  },
}
