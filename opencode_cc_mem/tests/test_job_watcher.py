"""Tests for the job watcher notice emission.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_job_watcher.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-servers" / "compchem-tools" / "src"))

from compchem_tools.tools import job_watcher


def rec(run_id="run_abc", lifecycle="completed"):
    return {
        "run_id": run_id,
        "tool": "haddock3",
        "lifecycle": lifecycle,
        "remote": {"local_run_dir": "/tmp/run", "remote_run_dir": "/remote/run"},
    }


def test_emit_writes_complete_notice(tmp_path):
    result = job_watcher.emit_job_notice(str(tmp_path), rec(), "COMPLETED", "success", "12345")
    assert result["emitted"] is True
    notice = json.loads(Path(result["path"]).read_text())
    assert notice["kind"] == "job_notice"
    assert notice["run_id"] == "run_abc"
    assert notice["state"] == "COMPLETED"
    assert notice["category"] == "success"
    assert notice["job_id"] == "12345"
    assert notice["event"]["source_id"] == "jobs:poller"
    assert notice["event"]["source_event_id"] == "run_abc:COMPLETED"
    assert notice["work_item_id"] is None


def test_duplicate_terminal_state_emits_once(tmp_path):
    first = job_watcher.emit_job_notice(str(tmp_path), rec(), "COMPLETED", "success", "1")
    second = job_watcher.emit_job_notice(str(tmp_path), rec(), "COMPLETED", "success", "1")
    assert first["emitted"] is True
    assert second == {"emitted": False, "reason": "duplicate", "path": first["path"]}
    assert len(list((tmp_path / "inbox" / "from-jobs").glob("*.json"))) == 1


def test_distinct_states_emit_distinct_notices(tmp_path):
    job_watcher.emit_job_notice(str(tmp_path), rec(), "RUNNING", "other", "1")
    result = job_watcher.emit_job_notice(str(tmp_path), rec(), "COMPLETED", "success", "1")
    assert result["emitted"] is True
    assert len(list((tmp_path / "inbox" / "from-jobs").glob("*.json"))) == 2


def test_deliberate_cancel_is_suppressed(tmp_path):
    result = job_watcher.emit_job_notice(str(tmp_path), rec(), "CANCELLED", "deliberate", "1")
    assert result["emitted"] is False
    assert not (tmp_path / "inbox" / "from-jobs").exists()


def test_poller_hook_is_wired():
    import inspect
    from compchem_tools.tools import poller
    source = inspect.getsource(poller.dispatch_terminal)
    assert "job_watcher.emit_job_notice" in source
