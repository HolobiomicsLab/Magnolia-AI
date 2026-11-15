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

Measured on 15 November with `deepseek/deepseek-v4.1-flash` through OpenRouter. With
`MAGNOLIA_OPENROUTER_PROVIDER` pinned to DeepInfra and GMICloud, a repeated identical critic call served
12,416 of 12,508 prompt tokens from cache and cost fell from USD 0.00184 to USD 0.00014. A new turn's
critic trace shared only the roughly 200-token system prompt, below the host's cache block, and therefore
reported zero cached tokens. The conversation extractor consistently cached 768 of 2,646 prompt tokens
(its roughly 850-token system prompt); because its second pass appends to the transcript, a pinned host can
then reuse the whole prior transcript prefix.

Without a host pin, nine calls were answered by seven hosts and none of the four repeat-eligible calls hit
a cache. Host pinning made caching work and also made judge replies reliable. The memory selector supplies
only the 15 best candidates per task (about 1,000 tokens), so candidate prompt order is immaterial in
practice.

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
