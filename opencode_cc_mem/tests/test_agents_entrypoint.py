"""Standalone helper-daemon import bootstrap tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "softwares" / "bin" / "magnolia-agents-daemon"


def test_daemon_direct_script_import_without_pythonpath(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    probe = (
        "import runpy; "
        f"runpy.run_path({str(SCRIPT)!r}, run_name='daemon_probe'); "
        "from campaign_runner.executors import registry; "
        "from campaign_runner import routing; "
        "print(registry.__file__); print(routing.__file__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert str(ROOT / "campaign_runner" / "executors" / "registry.py") in result.stdout
    assert str(ROOT / "campaign_runner" / "routing.py") in result.stdout
