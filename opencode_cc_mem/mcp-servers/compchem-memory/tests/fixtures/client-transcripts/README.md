# Client transcript fixtures (reduced)

One short session per client, recorded on 15 Nov 2026 in throwaway folders (Claude Code 2.1.325
with `--model haiku`, codex-cli 0.154.0 with `-s read-only`, Mistral Vibe 3.1.2 on its free default
model), prompt: say CHECKING, read `note.txt` (Claude also through a sub-agent reading `note2.txt`),
then summarise. No hooks, no trust or bypass flag.

Only conversation rows are kept, reduced to the fields the reply-text readers use. Everything else
was dropped: system prompts, instructions, environment context, attachments, effective
configuration, reasoning content of Codex, token usage. Home and temp paths are replaced by
`/HOME` and `/TMP`.

- `claude/session.jsonl`, `claude/subagents/agent-*.jsonl`: rows of `~/.claude/projects/<dir>/<sid>.jsonl`
  and its `<sid>/subagents/` files (the hook's `transcript_path` / `agent_transcript_path`).
- `codex/rollout.jsonl`: rows of `~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl`.
- `vibe/session/journal/*.jsonl`: rows of `~/.vibe/logs/session/unified/<sid>/journal/`; Vibe's
  hooks pass the session folder itself as `transcript_path`.
