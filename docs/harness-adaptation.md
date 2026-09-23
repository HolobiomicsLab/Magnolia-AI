# Adapting Magnolia to another harness

Connecting Magnolia's MCP servers gives another client memory and execution tools.
It does not reproduce the complete OpenCode integration. Instruction loading, task
retrieval, command capture, transcript ingestion and review all need an explicit
place in the new harness.

This guide describes the implementation at upstream `25b3d3b`, the plugin-import
correction `1eed4a7` and Bash parser correction `812b1b5` (11 September 2026). Other harnesses are integration
targets, not tested Magnolia configurations. Begin with the manual path below, then
add automation one responsibility at a time.

## Choose the integration level

| Level | What is available | What remains to implement or do explicitly |
|---|---|---|
| MCP plus manual protocol | Existing memory tools, execution service and project files | Load instructions, retrieve context, record decisions, review entries and write a handover |
| Harness adapter | The same services plus the target client's event API | Session/project identity, capture, retrieval timing, transcript export and review presentation |
| Direct Python/CLI integration | Python modules and logging/search commands | Tool schemas, error handling, capture and lifecycle; internal Python functions are not a stable external API |

MCP standardises discovery and invocation. The HTTP endpoint below is **MCP over
streamable HTTP**, not a general REST API. Calling internal Python functions directly
can bypass capture decorators and server lifecycle behaviour. Keep the source version
pinned if you take that route.

## Connect the services

First complete the [Python installation](getting-started.md#2-install-the-python-packages).
Choose one absolute project directory and a reviewed rule directory. The project may
live outside the Magnolia checkout. Do not depend on a client's implicit working
directory or copy another project's active-session marker.

| Service | Client connection | Process settings |
|---|---|---|
| `compchem-memory` | Local stdio command: `/absolute/Magnolia-AI/.venv/bin/python3 -m compchem_memory.server` | `MAGNOLIA_PROJECT_DIR=/absolute/project`, `MAGNOLIA_RULES_DIR=/absolute/Magnolia-AI/opencode_cc_mem/rules`, `MAGNOLIA_ROOT=/absolute/Magnolia-AI`; chosen memory provider/model and credentials |
| `compchem-tools` | `http://127.0.0.1:8001/mcp` | Start a local tools process with the environment below |

Translate those settings into the target client's documented MCP configuration; its
field names need not match OpenCode's `type`, `command` and `environment`. The shipped
memory entry point uses stdio. A client that supports only HTTP needs an additional
memory transport adapter; changing the URL does not create one.

Set `project_dir` explicitly on tool calls that accept it. The capture decorator uses
the call argument or the process working directory, whereas memory operations also
have a configured project default. Using the same absolute path avoids split records.
For `run_shell`, set `cwd` to the project as well: `magnolia-run` locates the notebook
by walking upward from that directory, not from the MCP `project_dir` argument.

### Start the tool server

For a diagnostic or manually supervised installation, use a dedicated terminal. Replace
the example paths with real absolute paths:

```bash
MAGNOLIA_CODE_DIR="/absolute/Magnolia-AI"
MAGNOLIA_WORK_DIR="/absolute/project"
mkdir -p "$MAGNOLIA_WORK_DIR/.magnolia"
cd "$MAGNOLIA_WORK_DIR"

export PATH="$MAGNOLIA_CODE_DIR/.venv/bin:$MAGNOLIA_CODE_DIR/opencode_cc_mem/softwares/bin:$PATH"
export MAGNOLIA_ROOT="$MAGNOLIA_CODE_DIR"
export MAGNOLIA_PROJECT_DIR="$MAGNOLIA_WORK_DIR"
export MAGNOLIA_RULES_DIR="$MAGNOLIA_CODE_DIR/opencode_cc_mem/rules"
export COMPCHEM_TOOLS_TRANSPORT=http
export COMPCHEM_TOOLS_HOST=127.0.0.1
export COMPCHEM_TOOLS_PORT=8001
"$MAGNOLIA_CODE_DIR/.venv/bin/python3" -m compchem_tools.server
```

Provide any needed model environment to this process before starting it. Keep the
terminal open; this foreground command does not install a service supervisor. On Linux,
the supplied `compchem-tools-daemon.sh` is the supervised alternative used by `magnolia`.
Do not start both on the same port. A different port requires matching the client URL.
Keep the local server on loopback; the supplied setup is not an authenticated remote
multi-user deployment.

The shell utility requirements still apply to `run_shell`/`magnolia-run` even when the
Python HTTP server starts successfully. Persistent HTTP avoids tying running jobs to
a client stdio pipe, but reconnection and job completion still need testing. Long work
belongs in background execution or the scheduler, not a prolonged foreground call.

## Establish the manual memory loop

1. Load the reviewed `AGENTS.md`, relevant `rules/*.md`, project `GOAL.md`, available
   `boot-context.md` and `audit-report.md`. Expose task skills through the new harness's
   own loader. MCP discovery alone does not load these documents.
2. On a new task, call `memory_get_context` with its description and the absolute project
   directory. Check the returned `content` and `sources`. For a pure recap, use the
   already-loaded handover as the supplied agent instructions prescribe.
3. Before a consequential action, retrieve and read relevant prior advice. Execute through
   the recorded route, preserving full artifacts and logs in the project.
4. Record decisions, resolved errors, unsuccessful approaches and parameter rationales
   with `memory_record_learning`. Review staging entries and use `memory_confirm` for
   those that should become project memory.
5. Surface consolidation and promotion proposals with the corresponding `memory_review_*`
   tools. Call `memory_apply_*` only for the human's selected proposals. A proposed rule
   is not accepted merely because a model drafted it.
6. Before stopping, write a short project handover: what completed, what failed, what is
   pending, source paths and the next action. A project `HANDOVER.md` loaded explicitly
   on the next session is a simple adaptation convention, not an automatically consumed
   Magnolia filename.

For example, these are **MCP argument objects**, not shell commands. Replace paths and
use the tool names shown by your client:

`memory_get_context`:

```json
{
  "task_description": "Resume the feature-table comparison and retrieve its QC decisions",
  "project_dir": "/absolute/project",
  "token_budget": 8000
}
```

`memory_record_learning`:

```json
{
  "title": "Sample identifiers must be aligned before comparison",
  "content": "Symptoms: the demo tables used different row orders. Cause: the comparison used row position. Fix: join on the checked sample_id mapping. Evidence: runs/demo/comparison.log and scripts/compare.py. Scope: this demo's input schema; other datasets need their own identifier check.",
  "entry_type": "error_resolution",
  "tags": ["table-comparison", "sample-identifiers"],
  "source": "manual",
  "project_dir": "/absolute/project"
}
```

This learning is illustrative; record it only if those observations were actually made.
Use the returned entry path/name when confirming, rather than inventing an identifier.

### CLI fallback

The fast search module provides lexical project/staging retrieval without a model:

```bash
/absolute/Magnolia-AI/.venv/bin/python3 -m compchem_memory.quick_search \
  'sample identifiers table comparison' --project-dir /absolute/project --k 4
```

It prints one JSON object per hit, with the source path and a provisional flag for
staging; no hits can produce empty output. Read the source before reusing advice.
This is not the full context-assembly pipeline. There is no implemented
`magnolia-memory get-context` CLI subcommand; use the MCP tool for that operation.

For commands run through the harness's own shell, use `magnolia-run <command...>` in
the project directory, or record the real command, exit status and output summary
with `magnolia-memory log-bash`. The [offline capture exercise](getting-started.md#3-check-capture-without-a-model)
shows the latter. Logging after an unwrapped action has a crash window and is a manual
fallback, not a guarantee of complete provenance.

`log-event` writes to a queue; it does not immediately append to the session log:

```bash
/absolute/Magnolia-AI/.venv/bin/magnolia-memory log-event \
  --project-dir /absolute/project --event-type decision \
  --data '{"summary":"Retain the checked baseline pending QC review","scope":"demo only"}'
/absolute/Magnolia-AI/.venv/bin/magnolia-memory sync-queue \
  --project-dir /absolute/project --delete-after
```

Inspect the queued event before ingesting if it needs review. Successful ingestion
with `--delete-after` removes the queue file; without that flag, repeating the command
replays the same events. This generic event is not automatically a confirmed learning.

## What the OpenCode plugins actually do

Current [OpenCode plugin documentation](https://opencode.ai/docs/plugins/) describes
lifecycle and tool hooks. Support depends on the client version, event shapes and
plugin loader; “supports hooks” alone is insufficient evidence of compatibility.
The repository does not pin a tested OpenCode version.
The action plugin also uses Bun's `import.meta.dir` for path resolution. Copying the
TypeScript file into a Node-based hook runner requires adapting that runtime detail
as well as the event API; a successful parse alone does not prove it can execute there.

| Plugin | Hook/event used by Magnolia | Actual effect | Evidence |
|---|---|---|---|
| [Session capture](../opencode_cc_mem/.opencode/plugins/magnolia-session-capture.ts) | `chat.message` plus generic `event` | Registers an OpenCode session ID and project mapping; the transcript is exported later | `.magnolia/opencode-sessions.jsonl` |
| [Task retrieval](../opencode_cc_mem/.opencode/plugins/magnolia-auto-retrieval.ts) | `chat.message` | Adds an instruction asking the model to call `memory_get_context`; does not itself retrieve | `.magnolia/auto-retrieval.jsonl` plus the actual subsequent tool call |
| [Action retrieval](../opencode_cc_mem/.opencode/plugins/magnolia-action-retrieval.ts) | `tool.execute.before` and `tool.execute.after` | Starts a search before selected tool calls, but attaches hits to their returned string **after execution** | `.magnolia/action-retrieval.jsonl` and modified tool result |
| [Claim critic](../opencode_cc_mem/.opencode/plugins/claim-critic.ts) | Generic `event`, filtered to `session.idle` | Optional model judgment of report versus tool trace; logs verdicts and displays flags | `.magnolia/claim-critic/<sessionID>.jsonl` |

Action retrieval is advisory feedback after an action, not a pre-execution gate.
If prior knowledge must change the parameters of the current action, retrieve it
explicitly before execution. The critic is also flag-only. It currently uses its own
DeepSeek call and environment, rather than inheriting every memory-provider option.
Enable it only with a suitable model and permission to send that report/tool trace.

A duplicate `join` import previously prevented the action plugin from parsing; it is
corrected at `1eed4a7`. A syntax check verifies that repair, not that OpenCode loads the
plugin or that the model follows an injected instruction. Plugin failures are often
swallowed, and an action-retrieval log records successful injections only. Silence can
mean no match, a disabled plugin, an unrecognised tool name or a failed hook.

## Diagnose OpenCode hooks

Use a synthetic project and record the OpenCode version and Magnolia revision.

1. **Load:** inspect OpenCode's logs for plugin import/runtime errors. Verify the installed
   event API, especially `chat.message`. Magnolia both lists plugins in its template and
   stores them in an auto-discovery directory; check that registration happens once.
2. **Project:** inspect the generated instruction paths and root `.magnolia/.active-project`.
   Check that the mapping and journals land in the intended project's notebook. The action
   plugin has a legacy project fallback; do not rely on absent configuration.
3. **Task:** send one new task. An `auto-retrieval.jsonl` injection row proves only that a
   directive was added. Inspect the tool trace for the actual context call and sources.
4. **Action:** create one reviewed, scoped synthetic learning. Invoke a matching action;
   inspect the result for its source path. Check the plugin's fixed `ACTION_TOOLS` names
   against the names emitted by the client. Also try an unrelated action and no-hit case.
5. **Transcript:** check the OpenCode session mapping and export that exact session using
   `opencode export <sessionID>` to a local file. A mapping row alone is not the transcript.
6. **Resume:** confirm a learning, close normally, reopen the project and retrieve it.
   Check the timestamp/content of the handover separately. Test an interrupted session too.
7. **Fallback:** if any automatic step fails, use the explicit loop above while diagnosing
   it. Keep the gap visible in the project handover rather than claiming automatic capture.

Do not send full session exports in a bug report: they may contain private research,
credentials or reasoning. A small synthetic reproduction is more useful.

## Transcript and session adaptation

[`opencode_ingest.py`](../opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/opencode_ingest.py)
uses OpenCode session mappings and `opencode export`; rolling handover also consumes
those exports. Merely connecting another MCP client does not supply its conversation.
Tool capture retains abbreviated arguments/results, so it cannot replace the complete
record of decisions or the original output files.

There is also a migration trap: when an OpenCode mapping exists and OpenCode plus a
memory model are available, [`startup_scan.py`](../opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/startup_scan.py)
selects OpenCode dialogue ingestion instead of tool-event fallback. A notebook used by
another harness may therefore keep scanning its old OpenCode history. Preserve the
original notebook, pilot in a fresh project, and implement explicit transcript-source
selection before claiming mixed-harness automatic distillation. Do not delete historical
mappings simply to force the fallback.

A full adapter should define stable project/session IDs, message ordering, export format,
per-session cursors and how interrupted exports are retried. Test long transcripts and
valid empty extraction separately from exporter/model failures; failures must not mark
unread content as processed. Capture only material the target harness exposes and permits
exporting. Review redaction for both secrets and scientific strings; a long sequence must
not disappear merely because it resembles a token.

## Acceptance checks for an adapter

| Scenario | Evidence required before describing support as tested |
|---|---|
| Fresh project and restart | Instructions loaded; reviewed learning retrieved with its source |
| One task / one action | Correct event identities; no duplicated capture or injection |
| Needed advice before mutation | Advice is available before the decision; an after-hook is not counted |
| Success, failure and interruption | Commands, exit state and incomplete artifacts remain traceable |
| Two projects | Records stay in their intended stores; no implicit active-project fallback |
| Structured tool result | Content survives without being flattened or silently dropped |
| Long or failed transcript export | No truncation or lost cursor range; failure remains retryable |
| Model/network unavailable | Explicit degradation, with raw evidence preserved |
| Human rejects a proposed rule | No unapproved rule application; rejection remains recorded |
| Domain changes | Irrelevant chemistry assumptions do not enter goals, retrieval or validation |

Record the client/version, Magnolia commit, OS, Python/dependency versions and observed
results. These are acceptance scenarios for future adapters. This pass verified a
fresh Python 3.12 installation, CLI examples, memory MCP record/confirm/restart/retrieval,
tools HTTP discovery and plugin parsing. It did not verify a complete OpenCode or
alternate-harness session, model distillation or scientific execution.
