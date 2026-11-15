# Live test campaign, 15 November 2026

Extended live tests of Magnolia's harness-neutral hooks after Plan 2, on a throwaway demo project:
Mistral Vibe 3.1.2 (about 25 sessions, the owner's Vibe plan), OpenCode 2.3.0 with a free OpenCode Zen
model (`opencode/deepseek-v4.1-flash-free`, no sign-in needed), and one hook-on session each in Claude Code
(Haiku) and Codex 0.154 (`gpt-5.6-luna`). Critic and memory calls went through OpenRouter
(`deepseek/deepseek-v4.1-flash`). Fixes are on branch `fix/vibe-campaign-20261115`; each round was reviewed
by an independent model and re-tested live. The acceptance record of the first live check is
[`harness-acceptance-2026-11.md`](harness-acceptance-2026-11.md).

## Client behaviours found (not Magnolia bugs; Magnolia now works around them)

| Client | Behaviour | Evidence | Magnolia's handling |
|---|---|---|---|
| Vibe 3.1.2 | Any `pre_agent` hook (even one printing nothing) makes every sub-agent fail: "child Session does not retain the requested generation" | isolated probes with a silent hook, a context hook and a prompt rewrite; without hooks children work | hook rendered with `match = "main"`; children then work |
| Vibe 3.1.2 | Post-tool `additional_context` is logged as appended but never reaches the model for MCP tools; built-in tools are fine | probe: marker seen after `read_file`, not after an MCP call | the note is carried to the same agent's next built-in tool or next prompt |
| Vibe 3.1.2 | MCP tools are called from `run_typescript` programs; a post-tool `deny` nulls `structured_content` | review of Vibe's code, recorded fixtures | MCP results are never altered |
| Vibe 3.1.2 | `post_agent` runs before the final reply is committed to the journal; `post_agent` carries no turn id | journal sequence; replay after the session recovered the reply | catch-up at the next prompt or session end, from the journal; turn id from the last prompt |
| Vibe 3.1.2, Codex 0.154 | stdio MCP servers get a minimal environment (no provider keys) | memory server logged `no_key` | Codex: `env_vars` lists the key names; Vibe: open (Plan 3) |
| Codex 0.154 | SessionStart additional context is cut to about 2,500 tokens ("…8056 tokens truncated…") | hook-on rollout | static rules in `developer_instructions`; dynamic part capped |
| Codex 0.154 | Reads skills from the project's `.agents/skills` | quoted a project skill | skills linked for Codex |
| Claude Code | `SubagentStop` fires before the child's final reply is written; async `Stop` never finishes in `-p` | hook-on transcript; earlier acceptance | synchronous Stop; probe and critic in a detached worker; child catch-up |
| OpenRouter | One model id is served by varying hosts; some reason past the judge's budget or return blanks | `finish_reason: length` with 53 characters; empty replies | `MAGNOLIA_OPENROUTER_PROVIDER` host pin; host logged; with DeepInfra/GMICloud pinned, every judge call returned JSON |
| OpenCode 2.3.0 | MCP calls go through a code-mode `execute` tool | `oc1` run | action retrieval still fired and reached the model |

## Magnolia defects found and fixed

- the critic and distillation could not see Magnolia's own injected context (false "high" flags; distillation
  re-learning its notes): delivered context is now recorded as `injected` rows, shown to the critic (capped),
  excluded from distillation;
- the parent's critic could not see a sub-agent's evidence: child rows in the turn's time window are included;
- denied tool calls were absent from the trace (critic false positive): recorded after the denial;
- `run_shell` truncated silently, mid-line: a marker line now states what was kept;
- job-notice run ids were cut from the left: shown in full up to 64 characters;
- Claude Code prompted for Magnolia's own MCP tools: they are pre-allowed in the rendered settings.

## Caching of Magnolia's own calls

Measured on 15 November with `deepseek/deepseek-v4.1-flash` through OpenRouter. With one sticky session per
role and no manual provider order, a repeated identical critic call served 12,416 of 12,489 prompt tokens
from cache. The extractor's second pass, which sends the transcript followed by an additive instruction,
served 2,560 of 2,661 prompt tokens from cache. Without a session id or provider order, nine calls were
answered by seven hosts and none hit cache. OpenRouter stickiness starts with the first successful request
carrying `session_id`, expires after 60 minutes without activity, and is disabled when a manual provider
`order` is supplied; a quantization filter alone preserves it.

Magnolia therefore sends a path-private `magnolia-<role>-<digest>` session id by default for memory and
critic calls. Set `MAGNOLIA_OPENROUTER_STICKY=0` (also `false`, `no` or `off`) to omit it. The default
provider quality floor accepts fp8, bf16, fp16, fp32 and endpoints whose quantization is unknown, avoiding
the fp4 hosts that returned degenerate JSON during the campaign. `MAGNOLIA_OPENROUTER_PROVIDER` replaces
that filter entirely, including with an explicit host order; Magnolia never drops a member-supplied value.
Invalid provider JSON warns once and falls back to the default floor. For a 400 or 404 that specifically says
`no endpoints found` or names quantization, Magnolia retries the default floor once without `provider` and
records the fallback. Context-length, token-limit and rate-limit errors do not trigger it.

OpenRouter caches DeepSeek, OpenAI, Gemini 2.5+, Grok, Moonshot and Z.AI models automatically. Anthropic
models require an explicit breakpoint, so Magnolia marks only the conversation extractor's transcript and
only when its second pass is enabled. Critic, handover merge, consolidation and promotion calls have no
reused prefix; marking them would pay Anthropic's 1.25x five-minute cache-write price without a later read
(a cache read costs 0.1x, and a prefix below the model minimum is neither cached nor charged extra).

The memory selector supplies only the 15 best candidates per task (about 1,000 tokens), so candidate prompt
order is immaterial in practice.

`.magnolia/llm-timing.jsonl` now adds any stated `prompt_tokens`, `cached_tokens`,
`cache_write_tokens`, `completion_tokens` and `cost` at the top level. Claim-critic verdict and error rows
store the same normalized fields under `usage`; omitted provider fields remain unknown rather than zero.

## Still open (Plan 3)

- Vibe's memory server has no way to receive the memory model's key without writing it to a file.
- In Vibe, a sub-agent that calls only MCP tools receives no action notes (Vibe defect above).
- OpenCode in `opencode run`: job notices are consumed but shown only as toasts (lost), and no critic row was
  written (the process likely exits first). Both are in the frozen OpenCode plugins (Tao's call).
- The hook-core doorbell now sends one letter per target per prompt. The frozen OpenCode plugin can still
  send duplicate letters; changing that plugin remains Tao's call.
- Distillation promoted a note from a member's own MCP server into project memory: provenance rules needed.
- Vibe and Codex show the member's global skills next to Magnolia's; this is the clients' intended discovery
  behaviour. Magnolia-rendered rule copies now use each client's native tool spelling, while OpenCode's source
  rules remain unchanged.
- Hook sessions are never sealed and are re-read in full on each sweep; proposed: size/mtime/offset cursors,
  close on session end or idle, archive after distillation.
