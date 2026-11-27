# Proposal: skills over MCP for Magnolia

Status: proposal for discussion, no code. Owner of every decision here: the Magnolia maintainer.
Nothing in it changes the pinned OpenCode runtime or the current deployment.

## Why

MCP now has a standard way for a server to offer skills: the `io.modelcontextprotocol/skills`
extension (SEP-2640), over the protocol revision 2026-07-28. The server answers `skills/list` and
`skills/get`, and each skill's `SKILL.md` is a resource (`skill://<name>/SKILL.md`). A host lists the
skills and loads one only when the task matches its description, as it does for skill folders today.

Collegium, the lab's shared-lessons service, tried this on 27 November 2026: `collegium mcp serve
--skills-project P` offers a member's adopted lessons as skills. In Claude Code (interactive sessions)
a lesson appeared as `collegium:<name>` and loaded. When its author withdrew it, it was simply no
longer listed, and there was no file copy left to erase.

For Magnolia this matters in two directions:

- **A. Magnolia as a client.** A Magnolia session (OpenCode) would read skills offered by an MCP server
  (Collegium's lessons first) instead of having files rendered into `.opencode/skills/`.
- **B. Magnolia as a server.** `compchem-memory` (or a small separate server) would offer Magnolia's
  own skills (`opencode_cc_mem/.opencode/skills/*`) to any host that speaks the extension. This fits
  the harness-neutral work (PRs 27 and 28), where Claude Code, Codex and Vibe would otherwise each need
  their own copy of the skill folders.

## What we know (evidence of 27 November 2026, synthetic data only)

- **OpenCode 2.3.0, the pinned runtime, has no MCP skills client.** The binary contains no
  `skills/list`, `skills/get` or `io.modelcontextprotocol/skills`. Against a stub server offering one
  skill, with the default setting it opened with `initialize` and called `tools/list` only. With
  `"protocol": "2026-07-28"` on the server entry it sent `server/discover` but never `skills/list`, and
  its skill registry kept the built-in skills only.
- **Upstream OpenCode 2.4.2 loads MCP skills behind a flag**, `OPENCODE_FLAGS=mcp_skills`
  (anomalyco/opencode PR 66201, merged 25 November). Two fixes came after it and were not in a release
  at that date: PR 66731 (ask once per skill version, not at every call) and PR 66870 (servers shared
  between sessions).
- **Side effects to expect:**
  - OpenCode drops the `instructions` of a server that offers skills.
  - The new `auto` protocol default also applies to Magnolia's own Python MCP servers
    (`compchem-memory`, `compchem-tools`): issue 66577.
- **Claude Code 2.1.331 and later consume MCP skills in interactive sessions only.** `claude -p`
  never sends `skills/list`. A skill is named `<server>:<name>`, and at most 100 are taken per server.

The raw evidence (the stub server, the OpenCode logs and the upstream excerpts) is kept outside this
repository and is available on request.

## What direction A would take

1. Move the OpenCode pin to a release that carries 2.4.2's `mcp_skills` and also 66731 and 66870.
   Then redo the evidence that depends on the runtime: the session-capture plugin
   (`magnolia-session-capture.ts`), the retrieval plugins, and the permission perimeter.
2. Turn the flag on, and add a `collegium` server entry with `"protocol": "2026-07-28"`.
3. Stop rendering the skill files for a project whose skills come from the server; otherwise each one
   shows twice.
4. Allow the `skill` permission on `collegium:*`. The new default `ask` would stall a headless run.
5. Teach the capture plugin to recognise `collegium:<name>` skill ids, so a session's journal records
   which served skill it loaded.
6. Check the two side effects above on `compchem-memory` and `compchem-tools` (their instructions and
   the protocol negotiation).

## What direction B would take

1. A server that answers `server/discover` with the skills extension and `resources`, plus
   `skills/list`, `skills/get` and `resources/read` of `skill://<name>/SKILL.md`.
   - Collegium's `src/collegium/mcp_server.py` is a working reference, under 400 lines, with its
     conformance tests.
   - To check: whether the Python MCP SDK used by `compchem-memory` can serve the extension, or
     whether these methods need their own small handler.
2. A rule for what is offered: the repository skills only; or, later, also learnings once they are
   confirmed. Staged learnings would not be offered.
3. A name rule, because the extension takes 1 to 64 characters of a-z, 0-9 and hyphens. Today's skill
   names already fit.
4. The same `SKILL.md` bytes as the folder, so that a host reading files and a host reading MCP see
   the same text.

## Proposed order

- **Phase 0, a probe with no change to the repository.** An isolated OpenCode 2.4.2 (its own
  `XDG_*` directories, the flag on), against the stub server and against a Collegium server on a
  throwaway hub. It records what is listed, what loads, what the capture plugin sees, and the two side
  effects.
- **Phase 1, the maintainer's decision:** A, B, both or neither, and when the pin moves.
- **Phase 2, the implementation** behind a configuration switch that is off by default, with its own
  tests in each package and an end-to-end check.

## Questions for the maintainer

1. Is A, B or both worth it for Magnolia, and in which order?
2. When could the pin move, and to which release?
3. For B: inside `compchem-memory`, or a separate server?
4. For the `skill` permission on served skills: `allow` for `collegium:*` only, or a wider rule?
