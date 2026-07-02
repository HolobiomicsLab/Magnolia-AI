"""Environment checking tools."""

import shutil
import subprocess
from typing import Any


def check_environment(
    tool_name: str,
    min_version: str | None = None,
    check_conda: bool = False,
) -> dict[str, Any]:
    """Verify that a given tool binary is available, report version, check conda env."""
    binary_names = {
        "haddock3": "haddock3",
        "haddock3-restraints": "haddock3-restraints",
        "haddock3-cfg": "haddock3-cfg",
        "haddock3-score": "haddock3-score",
        "gnina": "gnina",
        "xtb": "xtb",
        "acpype": "acpype",
        "gmx": "gmx",
        "orca": "orca",
        "p2rank": "p2rank",
        "cns": "cns",
    }

    binary = binary_names.get(tool_name, tool_name)
    result: dict[str, Any] = {
        "tool": tool_name,
        "binary": binary,
        "available": False,
        "version": None,
        "path": None,
        "conda_env": None,
    }

    which = shutil.which(binary)
    if which:
        result["available"] = True
        result["path"] = which
        result["version"] = _get_version(binary, tool_name)

    if check_conda:
        result["conda_env"] = _get_conda_env()

    return result


def _get_version(binary: str, tool_name: str) -> str | None:
    version_flags = {
        "haddock3": ["--version"],
        "haddock3-restraints": ["--version"],
        "haddock3-cfg": ["--version"],
        "gnina": ["--version"],
        "xtb": ["--version"],
        "acpype": ["--version"],
        "gmx": ["--version"],
        "orca": ["--version"],
        "p2rank": ["--version"],
    }

    flags = version_flags.get(tool_name, ["--version"])
    try:
        proc = subprocess.run(
            [binary] + flags,
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (proc.stdout + proc.stderr).strip()
        first_line = output.split("\n")[0] if output else None
        return first_line[:100] if first_line else None
    except Exception:
        return None


def _get_conda_env() -> str | None:
    return os.environ.get("CONDA_DEFAULT_ENV")


import os
