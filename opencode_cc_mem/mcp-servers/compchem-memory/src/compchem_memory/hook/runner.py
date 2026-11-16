"""Run Magnolia's hook from this repository under isolated Python."""

from __future__ import annotations

import os
import sys


def _repository_source() -> str:
    runner = os.path.abspath(__file__)
    relative = os.path.join(
        "mcp-servers", "compchem-memory", "src", "compchem_memory", "hook", "runner.py"
    )
    if runner != os.path.realpath(runner) or not runner.endswith(os.sep + relative):
        raise SystemExit("Magnolia hook runner must be a regular file in its repository source tree")
    return os.path.dirname(os.path.dirname(os.path.dirname(runner)))


def main() -> None:
    """Pin imports and bytecode to the repository-owned hook runtime."""
    source = _repository_source()
    prefix = os.environ.get("PYTHONPYCACHEPREFIX")
    if prefix:
        sys.pycache_prefix = prefix
    site_index = next(
        (index for index, path in enumerate(sys.path)
         if os.path.basename(path) in {"site-packages", "dist-packages"}),
        len(sys.path),
    )
    sys.path.insert(max(1, site_index), source)
    from compchem_memory.hook.__main__ import main as hook_main

    hook_main()


if __name__ == "__main__":
    main()
