# Contributing

Contributions are welcome, in the form of issues as much as of code. This document sets out where
things belong and the few conventions that are enforced.

## Reporting a problem

A report is most useful when it distinguishes what was asked from what happened. Please include:

- the prompt or command that started it, and what you expected;
- the relevant excerpt of the session journal (`<project>/.magnolia/sessions/`) or of the run
  directory (`runs/YYYY-MM-DD_name/`);
- for installation problems, the output of the verification command in
  [`docs/getting-started.md`](docs/getting-started.md);
- for tool problems, the daemon log (`opencode_cc_mem/logs/compchem-tools-http.log`).

Please redact usernames, account names and cluster paths before pasting. The repository has had to
untrack files for that reason before.

## Where a contribution belongs

The most common contribution adds an instrument. The shortest route is **a skill plus a thin typed
wrapper**, not free-form shell:

For a first pilot, the [domain adaptation guide](docs/domain-adaptation.md) starts with
an existing script and explicit learning capture before adding a reusable wrapper.
For another agent client, see [harness adaptation](docs/harness-adaptation.md), including
the OpenCode event dependencies and adapter acceptance checks.

| Contribution | Location | Note |
|---|---|---|
| A new instrument | `opencode_cc_mem/mcp-servers/compchem-tools/src/compchem_tools/tools/` and a skill in `opencode_cc_mem/.opencode/skills/<name>/SKILL.md` | The wrapper types the call; the skill carries the protocol, the failure modes and the verification checklist |
| A protocol for an existing instrument | `.opencode/skills/<name>/SKILL.md` | Loaded only when the task matches its description, so length is not a cost |
| Standing procedure | `opencode_cc_mem/rules/` | Read at **every** session start: every line is paid for each time. Keep it short, or it belongs in a skill |
| A cluster | `…/tools/clusters.yaml` for site facts; `~/.config/magnolia/clusters.yaml` for your own | See [`docs/hpc.md`](docs/hpc.md) |
| Memory behaviour | `…/compchem-memory/` | Changes here affect what every future session believes; argue the case in the issue first |
| Documentation | `README.md`, `docs/`, `WORKFLOW_GUIDE.md` | |

## What must never be committed

- API keys, tokens, passwords — of any provider, in any file.
- Usernames, personal account names, home paths, or any per-person cluster fact. These belong in
  `~/.config/magnolia/clusters.yaml`, which is outside the repository by design.
- Research data, run outputs, or any `.magnolia/` notebook. The repository carries the assistant,
  not the science.
- Machine-local launchers and symlinks under `softwares/bin/`, which are valid on one machine only.

The `.gitignore` enforces most of this, including a deny-by-default rule for the root `docs/`
directory with an explicit allow-list for the documentation set. Add your file to that list when you
add a document.

## Development

```bash
python3 -m venv .venv
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-tools
.venv/bin/python3 -m pip install -e opencode_cc_mem/mcp-servers/compchem-memory
```

Both packages install into the same environment; compchem-tools imports compchem-memory internally.

**Tests are run per package, from each package directory:**

```bash
cd opencode_cc_mem/mcp-servers/compchem-memory && ../../../.venv/bin/python3 -m pytest tests
cd opencode_cc_mem/mcp-servers/compchem-tools  && ../../../.venv/bin/python3 -m pytest tests
```

Both packages ship a `tests/conftest.py`, so collecting the two directories in one invocation fails
on a module-name collision (`ImportPathMismatchError`). Run them separately, as above; note also
that piping pytest into another command masks its exit status.

A test that patches away the effect it is meant to observe is worse than no test. If a fixture stubs
out a filesystem or network call, satisfy yourself that the assertion would still fail when the
behaviour regresses.

## Commits and pull requests

- **Conventional Commits**, with a scope: `feat(tools):`, `fix(memory):`, `docs:`, `test(memory):`,
  `refactor(skills):`, `fix(repo):`. The subject states what changed; the body states why.
- One concern per commit. Untracking a file and changing behaviour are two commits.
- Branch from `master`; name the branch for its purpose (`fix/…`, `docs/…`, `feat/…`).
- In the pull request, say what you verified and how. A statement that the suites pass is worth more
  when it names the platform and the Python version — several past defects were platform-specific.

## Licence

The project is distributed under the **MIT Licence with a Non-Military Clause**
([`LICENSE`](LICENSE)). Because clause 2 restricts a field of use, this is not the unmodified MIT
licence and is not an OSI-approved open-source licence; please do not describe the project as
MIT-licensed or as open source. By submitting a contribution you agree that it is distributed under
those same terms.
