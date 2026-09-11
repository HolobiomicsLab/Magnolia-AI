# Adapting Magnolia to another domain

Magnolia's project records, reviewed learnings and Git history can support computational
work beyond chemistry. The shipped protocols, tool recognition and several extraction
prompts still reflect computational chemistry. A domain adaptation therefore involves
more than changing the introductory prompt.

Start with one bounded workflow whose inputs, outputs and checks are already understood.
The [metabolomics example](use-cases.md#b-compare-preprocessing-choices-in-a-metabolomics-project)
uses existing scripts and explicit learning capture. It is a proposed application, not
a validated domain package. Changing the agent client is a separate task covered in
[harness adaptation](harness-adaptation.md).

## What to retain and what to adapt

| Layer | Retain | Adapt for the domain |
|---|---|---|
| Project memory | Session records, staged/confirmed entries, source pointers and corrections | Entry content, units, scope and relevance criteria |
| Rules | Short instructions for provenance, review and failure reporting | Domain-specific gates and limits on interpretation |
| Skills | On-demand, versioned written protocols | Inputs, method, dependencies, checks and failure modes |
| Execution | Recorded shell route, job lifecycle and structured errors | Program wrappers, argument validation and output parsers |
| Learning | Explicit `memory_record_learning` and project confirmation | Automatic extraction prompts and quality assessors |
| Evaluation | Restart/retrieval and provenance checks | Known-good, failed and out-of-scope examples judged by a domain expert |

The MCP names `compchem-memory` and `compchem-tools` remain the current identifiers.
Renaming them is unnecessary for an initial pilot and would also require updating
clients, plugin matching and tests. MCP is the tool interface; model-provider APIs
and the program APIs behind the wrappers are separate interfaces.

## 1. Write the workflow contract

In the project's `.magnolia/GOAL.md`, state:

- the question and what decision the result will inform;
- input locations, formats, identifiers, units and permitted data access;
- the existing protocol or script revision and required software;
- expected outputs and checks, including what makes a result unusable;
- the limits within which a recorded learning may be reused.

For example, a feature-table comparison needs a sample-ID mapping, QC labels and
declared normalization/blank-filtering criteria. A machine-learning experiment needs
the split definition, leakage checks, seeds and metric definitions. In both cases,
“the command completed” and “the result supports the decision” are separate claims.

Use your field's reviewed criteria. Magnolia should flag a missing threshold, unit or
reference dataset rather than choose one simply to make a run appear successful.

## 2. Begin with the recorded shell route

Keep the established script and place its invocation, configuration and logs in a
dated run directory. With `run_shell`, set **both** `project_dir` and `cwd` to the
absolute project path; its command logger finds `.magnolia/` by walking from `cwd`.
Use the background/scheduler path for work beyond the foreground time limit.

Ask for an explicit learning after the result has been checked. Useful entry types
are `error_resolution`, `failure_pattern`, `success_pattern` and `parameter_guidance`.
Each should identify evidence paths and the conditions under which it applies.
For a changed parameter, capture the old value, new value and reasoning at decision
time; a later transcript summary may omit that reasoning.

An unfamiliar command is still recordable. However, the CLI assessor skips commands
outside its recognised tool aliases. Automatic scientific assessment must therefore
remain unclaimed until a suitable assessor is implemented and verified.

## 3. Write one domain skill

Under the supplied OpenCode layout, put the protocol in
`opencode_cc_mem/.opencode/skills/<workflow-name>/SKILL.md`. For another harness, use
its documented skill-loading mechanism and verify that the body is actually loaded.

A starting template (replace the sample name and content):

```markdown
---
name: feature-table-review
description: Compare two processed feature tables with their sample metadata and QC criteria.
---

# Feature-table review

## Inputs
Identify both tables, sample-ID mapping, QC labels, parameter files and logs.
Require declared units and criteria before interpreting a comparison.

## Procedure
Retrieve prior scoped learning. Check inputs. Review the existing script and command.
Run it through the recorded execution path in a new run directory.

## Verification
Check sample alignment, missingness and the declared QC metrics against source files.
Distinguish absent outputs, failed processing and a scientifically unsuitable result.

## Record
Keep script/package versions, parameters, logs, tables and a decision with source paths.
Record the scope and unresolved questions before confirming a learning.
```

Keep task protocols in skills, observations in project memory, and only short
never-skip requirements in rules. Remove irrelevant chemistry doctrine from the
domain's instruction-loading configuration deliberately; do not erase shared source
files to make one project fit.

## 4. Add a typed wrapper when the workflow is stable

For repeated use, implement an instrument module in
`opencode_cc_mem/mcp-servers/compchem-tools/src/compchem_tools/tools/`, then register
its public tool in
[`compchem_tools/server.py`](../opencode_cc_mem/mcp-servers/compchem-tools/src/compchem_tools/server.py).
Follow the existing capture and project-resolution conventions, checking where records
actually land. Library calls that bypass those conventions need their own capture adapter.

The wrapper should validate input paths and schema, pass arguments without ambiguous
shell interpolation, report execution state separately from scientific validity, and
return output paths with typed metrics and units. Retain stderr and incomplete output
when a job fails. For long work, return a job identifier and a completion path instead
of holding a foreground MCP call open.

Tests should include valid inputs, malformed/missing inputs, a failed execution and
incomplete outputs. They must check the result against an independent expected outcome,
not merely reproduce the parser's own assumptions.

## 5. Audit the chemistry-specific assumptions

These are current source locations to inspect, not a promise that one central domain
configuration already controls them:

| Concern | Source | Required review |
|---|---|---|
| Generated project goal | [`magnolia`](../opencode_cc_mem/softwares/bin/magnolia), `write_goal_md` | Its prompt explicitly says computational chemistry; review generated goals |
| Automatic tool recognition | [`cli.py`](../opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/cli.py), `_TOOL_ALIASES` / `_detect_run_dir` | Add recognition and run-path handling for the new instrument |
| Scientific quality assessment | [`learning/assessor.py`](../opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/learning/assessor.py) | Parse the field's real metrics; do not reuse chemistry quality flags |
| Session/dialogue extraction | [`extraction.py`](../opencode_cc_mem/mcp-servers/compchem-memory/src/compchem_memory/extraction.py) | Chemistry wording, examples and exclusions must fit the new workflow |
| Action-related retrieval | [`magnolia-action-retrieval.ts`](../opencode_cc_mem/.opencode/plugins/magnolia-action-retrieval.ts), `ACTION_TOOLS` | Match new tool names/prefixes; current injection follows execution, so retrieve explicitly for a pre-action decision |
| Always-loaded instructions | [`AGENTS.md`](../opencode_cc_mem/AGENTS.md), [`rules/`](../opencode_cc_mem/rules/), [`opencode.json.template`](../opencode_cc_mem/opencode.json.template) | Retain memory/review discipline and select relevant domain rules |

Follow those functions into any downstream parser or prompt they invoke. A new wrapper
alone does not establish automatic learning for that instrument. Until the automatic
path is validated, record and confirm important findings explicitly and label the
integration as a pilot.

## 6. Check transfer before routine use

Use a small, approved example with a known outcome. Keep the same checks when changing
models or harnesses:

| Scenario | Passing evidence |
|---|---|
| Known-good run | Output and metrics agree with independent reference files |
| Failed run | Failure remains visible; no success learning is created from missing output |
| Parameter change | Old/new values and rationale are recorded with source paths |
| New session | Relevant confirmed learning is retrieved and its scope stated |
| Different cohort or instrument | The assistant recognises the scope mismatch and asks for the relevant checks |
| Incorrect prior learning | Correction remains traceable and superseded advice is not reused as current |
| Proposed shared rule | The human can inspect, reject or edit it; no unapproved application |

Archive the code, harness and dependency versions with these observations. Do not
promote a project-specific result into shared doctrine merely because it was repeated
in the same project. Wider applicability requires evidence from the conditions in
which the rule will actually be used.

The expected first deliverable is a small domain pilot: one protocol, one recorded
workflow, a checked result and a recoverable learning. A domain package can follow
once those parts work together.
