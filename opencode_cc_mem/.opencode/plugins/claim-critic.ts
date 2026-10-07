/**
 * claim-critic — FLAG-ONLY claim-vs-action auditor (Stage 1, observe only).
 *
 * When a Magnolia turn goes idle, an independent judge LLM reads the assistant's
 * report PLUS what the agent actually DID this turn (tool calls + their real
 * outputs) and flags any claim not supported by those actions — e.g. a computed
 * number / energy / ranking / mechanism that no tool actually produced, or
 * analysis steps the report implies but the trace shows were skipped.
 *
 * It changes NOTHING in the session: it only appends a verdict to
 *   <project>/.magnolia/claim-critic/<sessionID>.jsonl
 * and shows a toast when something is flagged. No blocking, no edits.
 *
 * Enable per-session by exporting MAGNOLIA_CRITIC=1 (the `magnolia --critic`
 * wrapper flag does this). Without it the plugin is a no-op.
 *
 * Why a judge that sees the tool OUTPUTS (not just summaries): validated in
 * projects/probability_entropy/derisk_judge.py — with only summaries the judge
 * over-flags legitimate computed values; with the real outputs it cleanly passes
 * good reports and flags skipped-steps garbage.
 *
 * Defensive by design: any failure is swallowed so auditing can never break the
 * opencode session.
 *
 * DUAL-SHAPE (2026-10-07, probe-verified on 1.18.34 + 2.0.6): plain-object
 * default export; v2 setup(ctx) subscribes to the event stream and reads
 * messages via ctx.session.context(); v1 server() keeps the original hooks
 * (event + client.session.messages). v1.18.34 also calls setup() with a
 * limited ctx — guarded. v2 has no server-side TUI domain: flagged verdicts
 * degrade to stderr (the JSONL log is the primary record either way).
 */
import { appendFileSync, mkdirSync } from "node:fs"
import { join } from "node:path"

const ENABLED = !!process.env.MAGNOLIA_CRITIC
const JUDGE_MODEL = process.env.MAGNOLIA_CRITIC_MODEL || "deepseek-v4-flash"
const MAX_OUTPUT_CHARS = 2000        // per-tool-output cap for most tools
const MAX_OUTPUT_CHARS_OLD = 500    // tighter cap for older non-evidence tools
const MAX_OUTPUT_CHARS_EVIDENCE = 0  // uncapped for tools that carry evidence (0 = no cap)
const MAX_TRACE_CHARS = 80000        // total trace cap (larger: full-session scope)
const MIN_REPORT_CHARS = 200         // skip trivial turns (acks, one-liners)

// Tools whose full output the judge needs to see (project memory, run history, etc.)
const EVIDENCE_TOOLS = new Set([
  "memory_get_context",
  "memory_get_run_history",
  "memory_search",
  "memory_scan_headers",
])

const SYSTEM =
  "You are a verification auditor for a scientific computing assistant. You are given " +
  "(1) the assistant's REPORT (from the CURRENT turn) and (2) the TOOL_TRACE of " +
  "ALL tools the assistant ran across the ENTIRE session (tools called, their inputs, " +
  "and their real outputs; older tools may have shorter output summaries). " +
  "Claims in the report may be supported by tools run in earlier turns, not just " +
  "the current one. Flag any claim in the report that is NOT supported by any action " +
  "in the trace — e.g. a computed number, energy, ranking, or interaction mechanism " +
  "that no tool output actually produced, or analysis steps the report implies but " +
  "the trace shows were skipped. Judge ONLY whether the report's claims are backed " +
  "by the actions/outputs in the trace, not abstract scientific correctness. " +
  'Respond with strict JSON: {"flag": boolean, "severity": "none|low|high", ' +
  '"unsupported_claims": [string], "why": string}.'

function criticDir(directory: string): string {
  // MAGNOLIA_PROJECT_DIR is exported by the magnolia wrapper (e.g. "projects/xiulian").
  const projectDir = process.env.MAGNOLIA_PROJECT_DIR
  return projectDir
    ? join(directory, projectDir, ".magnolia", "claim-critic")
    : join(directory, ".magnolia", "claim-critic")
}

async function judge(apiKey: string, report: string, traceText: string): Promise<any> {
  const res = await fetch("https://api.deepseek.com/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model: JUDGE_MODEL,
      temperature: 0,
      max_tokens: 500,
      response_format: { type: "json_object" },
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: `TOOL_TRACE:\n${traceText}\n\nREPORT:\n${report}\n\nReturn only the JSON.` },
      ],
    }),
  })
  const data: any = await res.json()
  return JSON.parse(data.choices[0].message.content)
}

// Shape-tolerant accessors: v1 messages are {info:{role}, parts:[...]}; v2
// ctx.session.context() returns SessionMessageInfo[] of the same family but
// with possible field drift — read defensively, never assume.
const roleOf = (m: any): string => String(m?.info?.role ?? m?.role ?? "").toLowerCase()
const partsOf = (m: any): any[] => m?.parts ?? []

async function auditSession(
  msgs: any[], sessionID: string, outDir: string, apiKey: string,
  toast: (title: string, message: string, variant?: string) => void,
) {
  try {
    if (!apiKey) return
    if (!Array.isArray(msgs) || msgs.length === 0) return

    // The current turn = everything after the last user message (for the report text).
    let lastUser = -1
    msgs.forEach((m, i) => { if (roleOf(m) === "user") lastUser = i })
    const turn = msgs.slice(lastUser + 1)

    // Collect the report text from the CURRENT turn only.
    const reportParts: string[] = []
    for (const m of turn) {
      if (roleOf(m) !== "assistant") continue
      for (const p of partsOf(m)) {
        if (p?.type === "text" && !p?.synthetic && p?.text) reportParts.push(p.text)
      }
    }

    const report = reportParts.join("\n").trim()
    if (report.length < MIN_REPORT_CHARS) return

    // Collect tools from the ENTIRE session so the judge sees evidence loaded
    // in earlier turns, not just the current one.
    const traceItems: any[] = []
    msgs.forEach((m, i) => {
      if (roleOf(m) !== "assistant") return
      const isCurrentTurn = i > lastUser
      for (const p of partsOf(m)) {
        if (p?.type !== "tool") continue
        const st = p?.state ?? {}
        const raw = typeof st?.output === "string" ? st.output : ""
        const isEvidence = EVIDENCE_TOOLS.has(p?.tool)
        let cap: number
        if (isEvidence) cap = MAX_OUTPUT_CHARS_EVIDENCE        // uncapped
        else if (isCurrentTurn) cap = MAX_OUTPUT_CHARS          // full cap
        else cap = MAX_OUTPUT_CHARS_OLD                        // tighter for older tools
        const output = cap ? raw.slice(0, cap) : raw
        const item: any = { tool: p?.tool, status: st?.status, input: st?.input, output }
        if (!isCurrentTurn) item.turn = "earlier"              // flag as cross-turn evidence
        traceItems.push(item)
      }
    })

    let traceText = JSON.stringify(traceItems, null, 1)
    if (traceText.length > MAX_TRACE_CHARS) traceText = traceText.slice(0, MAX_TRACE_CHARS) + "\n…(truncated)"

    const verdict = await judge(apiKey, report, traceText)

    // Observe-only: log EVERY verdict (flagged or not) so we can measure precision later.
    mkdirSync(outDir, { recursive: true })
    const rec = {
      ts: new Date().toISOString(),
      sessionID,
      model: JUDGE_MODEL,
      flag: !!verdict?.flag,
      severity: verdict?.severity ?? "none",
      unsupported_claims: verdict?.unsupported_claims ?? [],
      why: verdict?.why ?? "",
      n_tools: traceItems.length,
      report_preview: report.slice(0, 200),
    }
    appendFileSync(join(outDir, `${sessionID}.jsonl`), JSON.stringify(rec) + "\n")

    if (verdict?.flag) {
      toast("claim-audit", `⚠ (${rec.severity}) ${String(rec.why).slice(0, 140)}`, "warning")
    }
  } catch {
    /* never throw into opencode */
  }
}

export default {
  id: "claim-critic",

  /** opencode v2: event-stream subscription + in-process message reads. */
  async setup(ctx: any) {
    if (!ENABLED) return
    if (!ctx?.event?.subscribe) return // v1.18.34 also calls setup() with a limited ctx — v1 runs server()
    const apiKey = process.env.DEEPSEEK_API_KEY
    const outDir = criticDir(ctx.location?.directory ?? process.cwd())
    const toast = (title: string, message: string, variant?: string) => {
      const t = (ctx as any)?.tui?.showToast
      if (typeof t === "function") {
        t.call((ctx as any).tui, { body: variant ? { title, message, variant } : { title, message } }).catch(() => {})
      } else {
        console.error(`[claim-critic] ${title}: ${message}`)
      }
    }
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const ev: any = event
          // v2 vocabulary has no session.idle; the turn-terminal signal is
          // session.execution.succeeded with the id at durable.aggregateID.
          const turnEnd =
            ev?.type === "session.idle" || ev?.type === "session.execution.succeeded"
          const sid = ev?.durable?.aggregateID ?? ev?.properties?.sessionID ?? ev?.sessionID
          if (!turnEnd || !sid) continue
          // In-process capture (v2 exports lack assistant text + tool parts —
          // gate-1 finding): read messages through the session domain.
          const messages = await ctx.session.context({ sessionID: String(sid) }).catch(() => [])
          await auditSession(messages ?? [], String(sid), outDir, apiKey, toast)
        }
      } catch {
        /* aborted or stream error — never break the session */
      }
    })()
    return () => controller.abort()
  },

  /** opencode v1: original event hook + client.session.messages. */
  async server({ client, directory }: any) {
    if (!ENABLED) return {}
    const apiKey = process.env.DEEPSEEK_API_KEY
    const outDir = criticDir(directory)
    const toast = (title: string, message: string, variant?: string) => {
      client.tui.showToast({ body: { title, message, variant } }).catch(() => {})
    }
    return {
      event: async (input: any) => {
        if (input?.event?.type === "session.idle") {
          const sid = input?.event?.properties?.sessionID
          if (!sid) return
          try {
            const resp: any = await client.session.messages({ path: { id: sid } })
            const msgs: any[] = resp?.data ?? resp ?? []
            await auditSession(msgs, sid, outDir, apiKey, toast)
          } catch {
            /* never throw into opencode */
          }
        }
      },
    }
  },
}
