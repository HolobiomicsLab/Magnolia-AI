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
 * session id, runs `opencode export <id> --sanitize` to feed the REAL transcript
 * into distillation. See docs/superpowers/specs/2026-06-03-opencode-conversation-ingestion-design.md
 *
 * Defensive by design: any failure is swallowed so a capture problem can never
 * break the opencode session.
 */
import { appendFileSync, mkdirSync, existsSync, readFileSync } from "node:fs"
import { join, dirname } from "node:path"

import type { Plugin } from "@opencode-ai/plugin"

export const MagnoliaSessionCapture: Plugin = async ({ directory, worktree }) => {
  const mapPath = join(directory, ".magnolia", "opencode-sessions.jsonl")

  // session ids already recorded (so we append once per session, not per message)
  const seen = new Set<string>()
  try {
    if (existsSync(mapPath)) {
      for (const line of readFileSync(mapPath, "utf8").split("\n")) {
        if (!line.trim()) continue
        try { seen.add(JSON.parse(line).opencode_session_id) } catch { /* skip */ }
      }
    }
  } catch { /* first run / unreadable — start empty */ }

  const record = (sessionID: string) => {
    if (!sessionID || seen.has(sessionID)) return
    try {
      mkdirSync(dirname(mapPath), { recursive: true })
      const entry = {
        ts: new Date().toISOString(),
        opencode_session_id: sessionID,
        directory,
        worktree,
      }
      appendFileSync(mapPath, JSON.stringify(entry) + "\n")
      seen.add(sessionID)
    } catch { /* never throw into opencode */ }
  }

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
}
