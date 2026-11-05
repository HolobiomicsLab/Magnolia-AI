# The frontal Router (v0.1, PROVISIONAL)

The Router is the front door: it classifies, attaches, and proposes — it never
executes scientific work, advances campaign state, or promotes memory.

## Message flow

```
you -> Router.say(text)
        |
        +-- explicit wi:<id>  -> follow_up: context post (active) or new linked
        |                        Work Item (terminal). Stopped items are rejected.
        |
        +-- explicit project (@p / "in p:") -> handoff letter into that
        |   project's lane inbox (mailbox daemon resumes the lane), or a
        |   progress reply built from the Work Item store.
        |
        +-- "stop wi_<id>" -> engine.stop(); any later resume is rejected.
        |
        +-- nothing explicit -> a single non-demo registry project, else a
            clarifying question. Unknown projects are rejected, never guessed.
```

All actions are **observe-only for execution**: handoffs and posts stage work;
they do not run jobs. Execution still requires an approval + mandate.

## Robustness rules (enforced in code)

- **Registry from config only**: `~/.config/magnolia/router-projects.json` or
  `MAGNOLIA_AGENTD_PROJECTS`. No project names in repo code.
- **Explicit-reference-only attachment** (wi_/run_/project ids); no topic
  similarity routing.
- **Follow-up ownership**: one owner per Work Item; terminal items open a new
  linked Work Item, never a resurrection.
- **Human stop is authoritative**; a follow-up after stop is rejected.
- **Untrusted text is data**: injection-shaped inputs are flagged and routed as
  content only, never as commands.

## Replay evaluation (`demo-router`)

`python3 -m router_runner.agentd demo-router --db /tmp/router.db` runs a frozen
corpus of labeled cases (explicit/unknown/ambiguous projects, injection, active
and terminal follow-ups, stop/resume, progress, unknown Work Item) and reports:

- passed/total, missed_explicit_requests (must be 0), wrong_project (must be 0),
  unflagged_injection (must be 0), resumed_after_stop (must be false).

Use it before trusting any routing change.

## Leaving a project (`back to router`)

Exit is a user signal, never an automatic redirect. Saying **"back to router"**
(or "switch to", "new topic", "done here", "leave project") returns a
`return_to_router` reply and clears the handoff state, so your next message
routes fresh. It does **not** stop any running job: Work Items, lane sessions,
and memory persist. The router session itself is never closed.

## Using it with your project

The Router is project-agnostic; any project is supplied by configuration, not
code. Example with a placeholder project name (substitute locally):

```sh
export MAGNOLIA_AGENTD_PROJECTS="proj_a,proj_b,<your-project>"
export MAGNOLIA_AGENTD_OPERATOR="<you>"
```

The lane inbox is the configured project inbox (the mailbox daemon must be
running to consume handoff letters). For a pilot, the Router can hand off the
discussion to that project's lane, and the pilot Work Item then flows through
the existing approve/execute/park/poll/wake machinery.
