"""Standalone helper-daemon import bootstrap tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "softwares" / "bin" / "magnolia-agents-daemon"


def test_daemon_direct_script_import_without_pythonpath(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [str(SCRIPT), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "magnolia-agents-daemon" in result.stdout
