# Project-term scanner

Blocks known project terms and peptide sequences from entering the public
repository.

The deny list is private. It is never stored in this repository. Set
`PROJECT_TERMS_DENY_FILE` to a local file, or set `PROJECT_TERMS_DENY` in CI.
Do not commit the list.

## Run

```bash
PROJECT_TERMS_DENY_FILE=/path/to/private/deny.txt \
  python3 opencode_cc_mem/scripts/check_project_terms.py --all
```

## Pre-commit hook

```bash
git config core.hooksPath .githooks
```

The hook uses `~/.config/magnolia/project-terms-deny.txt` by default.

## CI

Set the repository secret `PROJECT_TERMS_DENY` to the private list, one term
per line. The workflow writes it to a temporary file and runs the scanner.
