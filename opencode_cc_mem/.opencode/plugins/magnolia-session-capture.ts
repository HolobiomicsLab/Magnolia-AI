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
import { join, dirname } from "node:path"

function projectMapPath(directory: string): string | null {
  // magnolia writes the active project name to .magnolia/.active-project
  // before exec-ing opencode, because opencode does not pass shell env vars
  // to plugin code. This marker file is the only reliable channel.
  try {
    const marker = join(directory, ".magnolia", ".active-project")
    const proj = readFileSync(marker, "utf8").trim()
    if (!proj) return null
    return join(directory, "projects", proj, ".magnolia", "opencode-sessions.jsonl")
  } catch { return null }
}

function makeRecorder(directory: string, worktree: string | undefined) {
  // per-project mapping — the one scan_and_distill reads
  const projPath = projectMapPath(directory)
  // shared root mapping — kept for the migration buffer; can be removed once
  // all projects have project-specific files and the scan fallback is gone
  const rootPath = join(directory, ".magnolia", "opencode-sessions.jsonl")

  // session ids already recorded (so we append once per session, not per message)
  const seen = new Set<string>()
  try {
    if (existsSync(rootPath)) {
      for (const line of readFileSync(rootPath, "utf8").split("\n")) {
        if (!line.trim()) continue
        try { seen.add(JSON.parse(line).opencode_session_id) } catch { /* skip */ }
      }
    }
  } catch { /* first run / unreadable — start empty */ }

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

  return (sessionID: string) => {
    if (!sessionID || seen.has(sessionID)) return
    // Write to the project-specific file (primary); also to the root
    // (migration buffer, so existing projects with no per-project file yet
    // don't silently lose new sessions before the scan fallback is removed).
    try {
      if (projPath) recordOne(projPath, sessionID)
      recordOne(rootPath, sessionID)
      seen.add(sessionID)
    } catch { /* never throw into opencode */ }
  }
}

/** Directory for the v2 in-process transcript dumps (next to the mapping). */
function transcriptsDir(directory: string): string {
  try {
    const marker = join(directory, ".magnolia", ".active-project")
    const proj = readFileSync(marker, "utf8").trim()
    if (proj) return join(directory, "projects", proj, ".magnolia", "opencode-transcripts")
  } catch { /* fall through to root */ }
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
    const dumpDir = transcriptsDir(directory)
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
          record(String(sid))
          const turnEnd =
            ev?.type === "session.idle" || ev?.type === "session.execution.succeeded"
          if (turnEnd && ctx?.session?.context) {
            const messages = await ctx.session
              .context({ sessionID: String(sid) })
              .catch(() => null)
            if (messages) dumpTranscript(dumpDir, String(sid), messages)
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
