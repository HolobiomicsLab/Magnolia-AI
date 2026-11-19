# Harness-neutral Magnolia — brief for Tao and his agents (16 November 2026)

Written by Louis-Félix's coding agent (Claude) at his request. It is meant to let Tao, or an agent working for him,
review the work and give feedback without the conversation that produced it. Nothing here has been pushed to the
Magnolia repository; the code travels as a Git bundle attached to the same Slack message.

## 1. How to read the code (for an agent)

The bundle holds branch `feat/harness-executors-p3` on top of Tao's tag `magnolia-v2.1.0-collegium` (`c285620`,
branch `exp/dreaming-pass`). In a Magnolia clone that has the tag:

```bash
git bundle verify magnolia-harness-neutral-2026-11-16.bundle
git fetch magnolia-harness-neutral-2026-11-16.bundle feat/harness-executors-p3:review/harness-neutral
git log --oneline c285620..review/harness-neutral | wc -l
git diff --stat c285620 review/harness-neutral -- . ':(exclude)*/tests/*' ':(exclude)*.md'
```

Read in this order (paths relative to `opencode_cc_mem/`):
1. `docs/harness-neutral-agents-design.md` — the design (intent, constraints, client capabilities, core, adapters,
   governor). Sections 1–3 and 8 are the ones that matter for a decision.
2. `docs/harness-neutral-agents-plan-p3.md` — the current plan; "Decisions for LFX before execution", "Global
   Constraints" and "Review Focus" carry every binding rule.
3. `docs/harness-probes-p3-live-2026-11.md` — what the real clients did on live runs (the evidence behind every
   "allowed / refused" below).
4. `docs/harness-campaign-2026-11-15.md` — the live campaign of 15 November and the defects it fixed.

Tests (three suites; nine failures in the campaign suite and four in the tools suite are pre-existing on our macOS
machine, unrelated to this work, and their assertions are untouched):

```bash
cd opencode_cc_mem
export PYTHONPATH=$PWD:$PWD/mcp-servers/compchem-memory/src:$PWD/mcp-servers/compchem-tools/src
python -m pytest -q tests/                                   # campaign runner, launcher, rules
(cd mcp-servers/compchem-memory && python -m pytest -q tests/)
(cd mcp-servers/compchem-tools && python -m pytest -q tests/)
```

At the bundle's head: campaign 657 passed (+9 pre-existing failures), memory 1,645 passed / 4 skipped, tools 339
passed (+4 pre-existing macOS failures).

## 2. Goal and the rules it keeps

Goal: Magnolia's agent and sub-agents run in the official clients the lab already pays for — Claude Code, Codex CLI,
Mistral Vibe — as well as OpenCode, each signed in with the member's own subscription, with the same memory, boot
context, retrieval, capture, claim critic, doorbell, job notices, campaigns and distillation.

Rules kept throughout (they are tested, not only stated):
- Magnolia never handles a subscription credential: no `claude --bare`, no `ANTHROPIC_API_KEY` / `CODEX_API_KEY`
  passed to a client, no alternate `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `VIBE_HOME`, no permission or hook-trust
  bypass, no `--trust`. Clients use their own normal login and normally persisted project trust.
- Consumption stays what a person would produce: every non-interactive run goes through a usage governor (section 4).
  Interactive sessions never take governor slots. Memory and critic calls stay on API billing (new setups prefer
  OpenRouter when `OPENROUTER_API_KEY` is available; existing explicit routes are preserved)
  and outside the governor.
- The OpenCode path does not change: its worker, reviewer, dedicated service, helper daemon and evaluator recipes are
  pinned by snapshot tests (`tests/snapshots/opencode/`); the only approved differences are the governor's waits,
  caps and quota stop, each tested separately.

## 3. What changed since `c285620`, by area

About 107 commits; about 17,400 added lines of code and scripts outside tests and docs (37,000 lines of tests and
fixtures).

| Area | Files (under `opencode_cc_mem/`) | What it does |
|---|---|---|
| Hook core | `mcp-servers/compchem-memory/src/compchem_memory/hook/` (`__main__`, `events`, `dispatch`, `policy`, `critic`, `action_retrieval`, `notices`, `doorbell`, `boot_context`, `transcript`, `client_transcripts`, …) | One Python implementation of the behaviours the OpenCode TypeScript plugins carried, behind one normalised event contract (`HookEvent`). |
| Client adapters | `hook/adapters/{claude,codex,vibe,json_adapter}.py` | Translate each client's native hook payloads and responses. |
| Renderers | `harness/render_{claude,codex,vibe,common}.py`, `harness/{files,neutral,protocol,ownership}.py` | Write each client's native config (hooks, MCP servers, rules, skills) for interactive and run-owned launches. |
| Launcher | `softwares/bin/magnolia` | `magnolia --harness opencode|claude|codex|vibe <project>`; OpenCode launch pinned by tests before the change. |
| Executors | `campaign_runner/executors/{contracts,registry,opencode,claude_code,codex,vibe,capabilities}.py` | One executor per client for worker, reviewer, helper and evaluator roles, chosen by route and refused unless the capability table accepts that client × role. |
| Governor | `campaign_runner/governor.py`, `process.py` | Admission, daily reservations, cooldown, shared quota holds, one receipt per physical invocation; governed launch through a small exec stub so a crash never leaves a paused process. |
| Durable quota state | `campaign_runner/state.py` | A campaign stage waits on a usage limit and resumes at the exact step, across crashes. |
| Roles and launch contract | `agents/{campaign-worker,campaign-reviewer,helper,evaluator}.yaml`, `campaign_runner/{roles,launch,routing}.py` | Each role's read roots, edit roots, MCP allowlist and delegation rule; an immutable per-run policy and client-native config, covered by a configuration digest. |
| Memory / LLM | `llm.py`, `server.py`, `opencode_ingest.py`, `transcript_source.py`, `handover.py`, `startup_scan.py` | Mistral, Z.ai and OpenRouter routes; OpenRouter prompt caching with sticky sessions per role (measured: 12,416 of 12,489 prompt tokens served from cache on a repeated critic call); distillation and handover from every client's transcripts; OpenCode 2.3.0 session tables. |
| OpenCode plugins | `.opencode/plugins/*.ts` | Configurable claim-critic judge (the largest change, about 170 lines); the others are a few lines each for OpenCode 2.3.0. |
| Tools | `compchem-tools/.../tools/shell.py` | `run_shell` states what it kept when it truncates output (no silent mid-line cut). |
| Router | `router_runner/evaluator.py` | The evaluator's OpenCode calls run with `--standalone`. |

## 4. Decisions taken (product owner: Louis-Félix) — each is open to your view

| Id | Decision | Where in the code |
|---|---|---|
| D1 | Governor per subscription: 2 runs at once, 50 a day, 60 turns a run, 30 s between runs. One run = one physical client invocation, including every resume and retry. Effective cap = min(role cap, governor cap). Policy `$XDG_CONFIG_HOME/magnolia/usage-policy.yaml`, ledger `$XDG_STATE_HOME/magnolia/usage-ledger.jsonl` (no prompts, replies or keys). | `governor.py` |
| D2 | Codex campaign worker and reviewer refused unless their safety gate passes (it did not, see section 5); Louis-Félix accepts a documented weaker guarantee only if no workaround exists. A workaround is now planned (section 6). | `executors/codex.py`, `capabilities-p3.json` |
| D3 | Vibe reaches memory through a project-bound loopback HTTP memory service (Vibe gives stdio MCP servers no provider key); the API key stays in the server's environment. In progress. | plan Task 9 |
| D4 | Vibe worker edits allowed only behind the strict pre-tool hook, after its live gate passes. | `executors/vibe.py` |
| R2 | Retries: a usage-limit signal overrides every retry layer; new clients get at most one same-session retry on an ordinary empty reply. OpenCode keeps its existing retries. | executors |
| — | Required role enforcement: when a role policy is required (`MAGNOLIA_HOOK_POLICY_REQUIRED=1`), the hook refuses every affected operation — reads included — on malformed input, unknown tools, invalid policy, unresolved paths or a hook crash (a small POSIX sh wrapper turns any hook failure into the client's block). Optional interactive policy keeps its former behaviour. | `hook/policy.py`, `hook/__main__.py`, `harness/render_common.py` |

## 5. What the live runs showed (normal trust, throwaway projects, owner's subscriptions)

- **Claude Code 2.1.325:** with the real executor, launch contract and hook, reviewer, helper and evaluator passed
  their gate; the worker passed with one accepted limit — it sees all of the memory server's tools and the hook
  refuses all but its three. A crashing hook (exit 1) lets every call through in Claude and Codex; the wrapper now
  blocks instead. Usage and cache counters are per invocation; a resumed run's cost comes from
  `model_usage_since_last_result`.
- **Codex 0.154:** no native read tool; the code-mode `exec` tool is not seen by hooks, but its programs cannot touch
  files, processes or the network on their own — every effect goes through hooked tool calls. Resume drops the model
  unless it is passed again; no native turn cap (a supervising bound is used). In code mode the `tools` object also
  exposes the account's ChatGPT connected-app tools (mail, drive, Slack); the hook refuses them and the launch must
  remove them.
- **Vibe 3.1.2:** `VIBE_DISABLE_TELEMETRY` is ignored (the switch is `VIBE_ENABLE_TELEMETRY=false`); with telemetry
  off the free model disappears and Vibe silently falls back to a paid one, so the executor verifies the model that
  ran. `--disabled-tools` does not remove the shell; only the strict hook blocks it. Three Vibe hook bugs are filed
  upstream: mistralai/mistral-vibe#1538, #1539, #1540.

## 6. Next

1. Fix round on the Codex and Vibe executors (in progress), then Task 8b: a Magnolia read/list/search tool in
   `compchem-tools`, confined server-side to the role's read roots and seen by the hook, so a Codex worker and reviewer
   can read with the shell blocked.
2. Task 9: the HTTP memory service for Vibe.
3. Phase C: campaigns, the helper daemon, the evaluator and interactive routing use the new executors; the memory
   server exposes only a role's allowlisted tools.
4. Phase D: short live campaigns on each client as acceptance; then delivery to you.

## 7. Questions for you (each has a default; nothing waits on you)

| Id | Question | Default meanwhile |
|---|---|---|
| TM-1 | Is the split acceptable: hook core in `compchem_memory/hook/`, renderers in `compchem_memory/harness/`, executors in `campaign_runner/executors/`? | Kept as is. |
| TM-2 | How do you want it delivered: one PR per plan (hook core P0–P1, adapters P2, executors P3), against `exp/dreaming-pass` or `v2-dev`? (`v2-dev` has 106 commits not in this base.) | Three PRs against `exp/dreaming-pass`, opened only when you say. |
| TM-3 | D1 governor numbers (2 / 50 / 60 / 30 per subscription) and the policy/ledger locations. | As decided. |
| TM-4 | May `compchem-tools` gain a read/list/search tool confined server-side to a role's read roots (for the Codex worker, Task 8b)? | Built on the branch, off unless a role allowlists it. |
| TM-5 | May the memory server expose only the tools a role allowlists (from an environment list), and gain an HTTP transport with a per-service bearer token for Vibe (D3)? | Built on the branch; stdio unchanged for OpenCode, Claude and Codex. |
| TM-6 | OpenRouter as the preferred route for new critic and memory setups when its key is available, with prompt caching and an optional host pin (`MAGNOLIA_OPENROUTER_PROVIDER`). | Existing routes are preserved; Mistral and Z.ai remain selectable. Explicit OpenRouter failures degrade visibly unless a direct fallback is configured. |

Answers can go in the Slack thread or as review comments once the PRs exist. For anything about code you own, it is
your call.
