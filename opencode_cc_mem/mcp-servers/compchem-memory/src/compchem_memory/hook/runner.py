"""Run Magnolia's hook from this repository under isolated Python."""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Pin imports and bytecode to the repository-owned hook runtime."""
    prefix = os.environ.get("PYTHONPYCACHEPREFIX")
    if prefix:
        sys.pycache_prefix = prefix
    package = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    source = os.path.dirname(package)
    sys.path.insert(0, source)
    from compchem_memory.hook.__main__ import main as hook_main

    hook_main()


if __name__ == "__main__":
    main()
