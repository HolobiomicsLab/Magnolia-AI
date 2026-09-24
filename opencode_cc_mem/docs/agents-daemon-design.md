# Magnolia agents daemon — design (2026-09-24)

Two helper agents — **literature** (papers in → digests out) and **xiulian**
(idea tickets → prototypes on throwaway branches) — that come alive when
Magnolia starts, sleep while idle, and act only when a request letter arrives
in their project mailbox. Event-driven, not LLM-looping: idle cost ≈ zero.

## Components

1. **Daemon** — `softwares/bin/magnolia-agents-daemon` (python3, stdlib only).
   - Started by the `magnolia` launcher; single-instance via flock
     (`~/.magnolia/agents-daemon.lock`); a second start exits quietly.
   - Loop: every 10 s scan the watched mailboxes; `--once` for tests.
   - Mailbox = `<root>/opencode_cc_mem/projects/<agent>/inbox/` (recursive).
     A **task** is a file named `*.task.md` whose text contains a line
     `status: open` (case-insensitive). Free-form letters (no `.task.md`
     suffix) are NOT auto-run — the human-mediated intercom protocol from
     2026-09-24 is untouched.
   - Per task: build a self-contained prompt (the letter + "work only inside
     your project and the inbox"), run `opencode run` with cwd = the agent's
     project dir, env `MAGNOLIA_PROJECT_DIR=projects/<agent>`,
     `MAGNOLIA_ROOT=<root>`, timeout 30 min. Write
     `<task_stem>_reply.md` beside the letter; set the letter's status to
     `done` (or `error` on failure, with the error in the reply); record the
     file hash in the state file so a done letter is never re-run — **but an
     edited letter reopens** (new hash = new work request).
   - One task at a time (sequential queue). Kill switch `MAGNOLIA_AGENTS=0`.
   - State + log: `<root>/.magnolia-agents/{state.json, daemon.log}`.

2. **Doorbell** — `.opencode/plugins/magnolia-agent-doorbell.ts`.
   - Hook `chat.message` (mutable `output.parts`, same proven surface as
     magnolia-auto-retrieval). On a user message containing `@literature` or
     `@xiulian`, the rest of that line becomes the request; the plugin writes
     the `.task.md` letter into the target mailbox (from = current project,
     resolved via `.active-project`), replaces the mention with a short
     "[sent to @agent]" note, and toasts.
   - Kill switch `MAGNOLIA_DOORBELL=0`. Defensive: any failure is swallowed.

3. **Return path (manual in v1).** The helper writes its reply letter; a
   `from-literature/...idea.ticket.md` in xiulian's inbox is triaged by the
   next xiulian session; accepted ideas become `exp/idea-*` prototype
   branches. Nothing merges without the user.

## Safety rails

- Helpers are event-driven; no timers that call LLMs, no self-modification.
- Literature writes only inside its own project + the two inboxes.
- Prototypes live on throwaway branches; merging = human decision.
- Kill switches: `MAGNOLIA_AGENTS=0` (daemon), `MAGNOLIA_DOORBELL=0`
  (doorbell). The smoke-detector checklist can later watch daemon.log.

## Known limitation (v1, documented)

The headless run resolves `opencode.json` by walking up from the agent's
project dir — i.e. the shared render of the LAST launched project, whose MCP
`environment` may pin compchem-memory to a different project. The task prompt
is therefore self-contained and instructs the helper not to use project
memory tools. Proper fix = per-agent project config render (folds into the
opencode-v2 migration item in todo.md).

## Findings from the first live smoke (2026-09-24, all folded into v1)

1. **Always pass `--model` explicitly.** Bare `opencode run` falls back to
   the GLOBAL config's default model, which here names a retired model
   (`zai-coding-plan/glm-4.6`) → ProviderModelNotFoundError. Default:
   `zai-coding-plan/glm-5.3-flash`; override with `MAGNOLIA_AGENTS_MODEL`.
   (Fixing the stale global default is a user-level todo — it breaks any
   bare `opencode run`.)
2. **Helpers answer via stdout, never by writing files.** Headless runs
   cannot approve file-write permission asks; asking the helper to write the
   reply file made it fight the permission wall for ~4 min and end with
   empty output. The prompt now forbids file writes; the daemon captures the
   final message and writes the reply itself (fallback: if the file already
   exists, the agent's own write is kept).
3. **A headless session takes ~2–4 min regardless of task size** (plugin +
   MCP-server startup dominates). Timeout is 30 min; one task at a time.
4. DeepSeek thinking models can yield empty visible output in this surface
   (known quirk, entry `20260828_150519_679472`) — another reason for the
   GLM default.

## Test plan

- Unit (pytest, `opencode_cc_mem/tests/test_agents_daemon.py`): task
  discovery (open/done/non-task), dedup via state, reopen-on-edit, reply +
  status rewrite, runner-failure → error status, runner mock.
- Integration: real end-to-end smoke — a trivial task for the literature
  agent through the real `opencode run`; assert the reply file appears.
- Doorbell: transpile check (bun) + careful review; runtime check = first
  real `@literature` use.
