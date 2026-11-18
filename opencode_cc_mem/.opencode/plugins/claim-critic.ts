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
 * JUDGE PROVIDER (2026-11-15). Resolution mirrors the memory client
 * (compchem_memory/llm.py):
 *   1. MAGNOLIA_CRITIC_PROVIDER — deepseek | openai | anthropic | kimi |
 *      openrouter | mistral | zai;
 *   2. otherwise the prefix of MAGNOLIA_CRITIC_MODEL (deepseek*, claude*,
 *      gpt-, kimi, mistral, devstral and the other Mistral families, glm,
 *      and vendor/model ids → openrouter);
 *   3. otherwise the first key present, DeepSeek first (the historical
 *      behaviour), then Anthropic, OpenAI, Kimi, OpenRouter, Mistral, Z.ai.
 * MAGNOLIA_CRITIC_MODEL overrides the provider's default model,
 * MAGNOLIA_CRITIC_API_KEY its key and MAGNOLIA_CRITIC_BASE_URL its endpoint
 * (any OpenAI-compatible service through provider "openai"). The provider
 * base-URL variables of the memory client (DEEPSEEK_BASE_URL, ZAI_BASE_URL, …)
 * are honoured too. A judge from another model family than the main agent
 * keeps the audit independent. Failed judge calls are appended to
 * <claim-critic dir>/errors.jsonl so a misconfigured critic is visible.
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
 *
 * OpenCode 2.3.0 (probed 2026-11-14): loads; setup() completed without a
 * "failed to load plugin" warning. Not yet observed acting on 2.3.0.
 */
import { appendFileSync, chmodSync, existsSync, mkdirSync } from "node:fs"
import { join } from "node:path"

const ENABLED = !!process.env.MAGNOLIA_CRITIC
const MAX_OUTPUT_CHARS = 2000        // per-tool-output cap for most tools
const MAX_OUTPUT_CHARS_OLD = 500    // tighter cap for older non-evidence tools
const MAX_OUTPUT_CHARS_EVIDENCE = 0  // uncapped for tools that carry evidence (0 = no cap)
const MAX_TRACE_CHARS = 80000        // total trace cap (larger: full-session scope)
const MIN_REPORT_CHARS = 200         // skip trivial turns (acks, one-liners)
const JUDGE_TIMEOUT_MS = 60_000       // matches compchem_memory.llm.call_route

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

type Schema = "openai" | "anthropic"
type ProviderSpec = { base: string; baseEnv: string; keys: string[]; model: string; schema: Schema }

// Defaults match compchem_memory/llm.py so both background roles behave alike.
const PROVIDERS: Record<string, ProviderSpec> = {
  deepseek: { base: "https://api.deepseek.com/v1", baseEnv: "DEEPSEEK_BASE_URL", keys: ["DEEPSEEK_API_KEY"], model: "deepseek-flash", schema: "openai" },
  anthropic: { base: "https://api.anthropic.com/v1", baseEnv: "ANTHROPIC_BASE_URL", keys: ["ANTHROPIC_API_KEY"], model: "claude-haiku-4-5-20251001", schema: "anthropic" },
  openai: { base: "https://api.openai.com/v1", baseEnv: "OPENAI_BASE_URL", keys: ["OPENAI_API_KEY"], model: "gpt-4o-mini", schema: "openai" },
  kimi: { base: "https://api.kimi.com/coding/v1", baseEnv: "KIMI_BASE_URL", keys: ["KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY"], model: "k3", schema: "anthropic" },
  openrouter: { base: "https://openrouter.ai/api/v1", baseEnv: "OPENROUTER_BASE_URL", keys: ["OPENROUTER_API_KEY"], model: "deepseek/deepseek-v4.1-flash", schema: "openai" },
  mistral: { base: "https://api.mistral.ai/v1", baseEnv: "MISTRAL_BASE_URL", keys: ["MISTRAL_API_KEY"], model: "mistral-small-latest", schema: "openai" },
  zai: { base: "https://api.z.ai/api/paas/v4", baseEnv: "ZAI_BASE_URL", keys: ["ZHIPU_API_KEY", "ZAI_API_KEY"], model: "glm-5.3-flash", schema: "openai" },
}
const AUTODETECT_ORDER = ["deepseek", "anthropic", "openai", "kimi", "openrouter", "mistral", "zai"]
const MODEL_PREFIXES: [string, string][] = [
  ["deepseek", "deepseek"], ["claude", "anthropic"], ["gpt-", "openai"], ["o1", "openai"],
  ["o3", "openai"], ["o4", "openai"], ["chatgpt", "openai"], ["kimi", "kimi"], ["k3", "kimi"],
  ["moonshot", "kimi"], ["mistral", "mistral"], ["ministral", "mistral"], ["magistral", "mistral"],
  ["devstral", "mistral"], ["codestral", "mistral"], ["pixtral", "mistral"],
  ["open-mistral", "mistral"], ["glm", "zai"],
]

type JudgeRoute = { provider: string; model: string; url: string; key: string; schema: Schema }

const env = (name: string): string => (process.env[name] ?? "").trim()
const firstKey = (spec: ProviderSpec): string => spec.keys.map(env).find(Boolean) ?? ""

function normaliseBase(raw: string): string {
  const base = raw.replace(/\/+$/, "")
  return /\/v\d+$/.test(base) ? base : `${base}/v1`
}

function resolveJudge(): JudgeRoute | null {
  const requestedModel = env("MAGNOLIA_CRITIC_MODEL")
  const explicit = env("MAGNOLIA_CRITIC_PROVIDER").toLowerCase()
  let provider = PROVIDERS[explicit] ? explicit : ""
  if (!provider && requestedModel) {
    const m = requestedModel.toLowerCase()
    if (m.includes("/")) provider = "openrouter"
    else provider = MODEL_PREFIXES.find(([prefix]) => m.startsWith(prefix))?.[1] ?? ""
  }
  if (!provider) provider = AUTODETECT_ORDER.find((name) => firstKey(PROVIDERS[name])) ?? ""
  if (!provider) return null
  const spec = PROVIDERS[provider]
  const key = env("MAGNOLIA_CRITIC_API_KEY") || firstKey(spec)
  if (!key) return null
  const rawBase = env("MAGNOLIA_CRITIC_BASE_URL") || env(spec.baseEnv)
  const base = rawBase ? normaliseBase(rawBase) : spec.base
  const path = spec.schema === "anthropic" ? "/messages" : "/chat/completions"
  return { provider, model: requestedModel || spec.model, url: base + path, key, schema: spec.schema }
}

/** The verdict object inside a reply that may carry code fences or prose. */
function parseVerdict(text: string): any {
  const start = text.indexOf("{")
  const end = text.lastIndexOf("}")
  if (start < 0 || end <= start) throw new Error("judge reply has no JSON object")
  return JSON.parse(text.slice(start, end + 1))
}

function criticDir(directory: string): string {
  // Project-root mode: cwd is the project store itself.
  if (
    process.env.MAGNOLIA_PROJECT_WORKSPACE === "1" ||
    existsSync(join(directory, ".magnolia", "GOAL.md")) ||
    existsSync(join(directory, ".magnolia", "boot-context.md"))
  ) {
    return join(directory, ".magnolia", "claim-critic")
  }
  // Workspace mode: MAGNOLIA_PROJECT_DIR may be absolute or relative.
  const projectDir = process.env.MAGNOLIA_PROJECT_DIR
  if (projectDir && projectDir.startsWith("/")) {
    return join(projectDir, ".magnolia", "claim-critic")
  }
  return projectDir
    ? join(directory, projectDir, ".magnolia", "claim-critic")
    : join(directory, ".magnolia", "claim-critic")
}

async function judge(route: JudgeRoute, report: string, traceText: string): Promise<any> {
  const user = `TOOL_TRACE:\n${traceText}\n\nREPORT:\n${report}\n\nReturn only the JSON.`
  let headers: Record<string, string>
  let body: any
  if (route.schema === "anthropic") {
    // Anthropic Messages schema (Anthropic, Kimi-for-Coding). Kimi accepts
    // either auth header, Anthropic needs x-api-key.
    headers = {
      "Content-Type": "application/json",
      "x-api-key": route.key,
      "anthropic-version": "2023-06-01",
      ...(route.provider === "kimi" ? { Authorization: `Bearer ${route.key}` } : {}),
    }
    body = {
      model: route.model, max_tokens: 500, temperature: 0, system: SYSTEM,
      messages: [{ role: "user", content: user }],
      ...(route.provider === "kimi" ? { thinking: { type: "disabled" } } : {}),
    }
  } else {
    headers = {
      "Content-Type": "application/json",
      Authorization: `Bearer ${route.key}`,
      ...(route.provider === "openrouter" ? { "HTTP-Referer": "https://magnolia.local", "X-Title": "Magnolia" } : {}),
    }
    body = {
      model: route.model, temperature: 0, max_tokens: 500,
      response_format: { type: "json_object" },
      messages: [{ role: "system", content: SYSTEM }, { role: "user", content: user }],
      // Reasoning models can spend the whole 500-token budget thinking and
      // return empty content; DeepSeek and GLM accept this switch.
      ...(route.provider === "deepseek" || route.provider === "zai" ? { thinking: { type: "disabled" } } : {}),
    }
  }
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), JUDGE_TIMEOUT_MS)
  let data: any
  try {
    const res = await fetch(route.url, {
      method: "POST", headers, body: JSON.stringify(body), signal: controller.signal,
    })
    if (!res.ok) throw new Error(`HTTP ${res.status} from ${route.provider}`)
    data = await res.json()  // the body read stays under the same timeout
  } finally {
    clearTimeout(timeout)
  }
  const text = route.schema === "anthropic"
    ? (data?.content ?? []).find((b: any) => b?.type === "text")?.text ?? ""
    : data?.choices?.[0]?.message?.content ?? ""
  return parseVerdict(String(text))
}

function privateDir(path: string) {
  mkdirSync(path, { recursive: true, mode: 0o700 })
  try { chmodSync(path, 0o700) } catch { /* best effort */ }
}

function privateAppend(path: string, line: string) {
  appendFileSync(path, line, { mode: 0o600 })
  try { chmodSync(path, 0o600) } catch { /* best effort */ }
}

function logError(outDir: string, sessionID: string, route: JudgeRoute, err: unknown) {
  try {
    privateDir(outDir)
    const rec = {
      ts: new Date().toISOString(), sessionID, provider: route.provider, model: route.model,
      error: String((err as any)?.message ?? err).slice(0, 300),
    }
    privateAppend(join(outDir, "errors.jsonl"), JSON.stringify(rec) + "\n")
  } catch {
    /* never throw into opencode */
  }
}

// Shape-tolerant accessors: v1 messages are {info:{role}, parts:[...]}; v2
// ctx.session.context() returns SessionMessageInfo[] of the same family but
// with possible field drift — read defensively, never assume.
const roleOf = (m: any): string => String(m?.info?.role ?? m?.role ?? "").toLowerCase()
const partsOf = (m: any): any[] => m?.parts ?? []

async function auditSession(
  msgs: any[], sessionID: string, outDir: string, route: JudgeRoute | null,
  toast: (title: string, message: string, variant?: string) => void,
) {
  if (!route) return
  try {
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

    let verdict: any
    try {
      verdict = await judge(route, report, traceText)
    } catch (err) {
      logError(outDir, sessionID, route, err)
      return
    }

    // Observe-only: log EVERY verdict (flagged or not) so we can measure precision later.
    privateDir(outDir)
    const rec = {
      ts: new Date().toISOString(),
      sessionID,
      provider: route.provider,
      model: route.model,
      flag: !!verdict?.flag,
      severity: verdict?.severity ?? "none",
      unsupported_claims: verdict?.unsupported_claims ?? [],
      why: verdict?.why ?? "",
      n_tools: traceItems.length,
      report_preview: report.slice(0, 200),
    }
    privateAppend(join(outDir, `${sessionID}.jsonl`), JSON.stringify(rec) + "\n")

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
    const route = resolveJudge()
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
          // (2.0.6. The 2.3.0 binary defines both names; which one the
          // stream delivers is not established — keep both.)
          const turnEnd =
            ev?.type === "session.idle" || ev?.type === "session.execution.succeeded"
          const sid = ev?.durable?.aggregateID ?? ev?.properties?.sessionID ?? ev?.sessionID
          if (!turnEnd || !sid) continue
          // In-process capture (v2 exports lack assistant text + tool parts —
          // gate-1 finding): read messages through the session domain.
          const messages = await ctx.session.context({ sessionID: String(sid) }).catch(() => [])
          await auditSession(messages ?? [], String(sid), outDir, route, toast)
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
    const route = resolveJudge()
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
            await auditSession(msgs, sid, outDir, route, toast)
          } catch {
            /* never throw into opencode */
          }
        }
      },
    }
  },
}
