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

function projectMapPath(directory: string): string | null {
  // magnolia exports MAGNOLIA_PROJECT_DIR (e.g. "projects/obp") before
  // exec-ing opencode, so the opencode process — and this plugin — see it.
  const pd = process.env.MAGNOLIA_PROJECT_DIR
  if (!pd) return null
  return join(directory, pd, ".magnolia", "opencode-sessions.jsonl")
}

export const MagnoliaSessionCapture: Plugin = async ({ directory, worktree }) => {
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

  const record = (sessionID: string) => {
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
