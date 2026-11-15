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

## Still open (Plan 3)

- Vibe's memory server has no way to receive the memory model's key without writing it to a file.
- In Vibe, a sub-agent that calls only MCP tools receives no action notes (Vibe defect above).
- OpenCode in `opencode run`: job notices are consumed but shown only as toasts (lost), and no critic row was
  written (the process likely exits first). Both are in the frozen OpenCode plugins (Tao's call).
- The doorbell sends two letters when a prompt mentions `@literature` twice (shared with the OpenCode plugin).
- Distillation promoted a note from a member's own MCP server into project memory: provenance rules needed.
- `rules/magnolia.md` names OpenCode's tool spelling (`compchem-tools_run_shell`); Vibe and Codex also show the
  member's ~90 global skills next to Magnolia's.
- Hook sessions are never sealed and are re-read in full on each sweep; proposed: size/mtime/offset cursors,
  close on session end or idle, archive after distillation.
- Codex's critic still runs inside its turn-end hook; a Vibe child's consecutive turns need a stable turn id.
