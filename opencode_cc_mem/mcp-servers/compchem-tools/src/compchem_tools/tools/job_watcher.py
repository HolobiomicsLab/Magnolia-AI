"""Job watcher: durable job notices for the always-on agent system.

The poller already detects terminal transitions centrally (dispatch_terminal).
This module is the ONLY new machinery needed to turn that into a wake-up
event: one JSON notice per (run_id, terminal state), written atomically into
<project>/inbox/from-jobs/. The mailbox daemon (Option A wake-up) or agentd
can then resume the owning worker — no worker ever has to poll.

Idempotency: the notice filename carries (run_id, state), so re-sweeps and
restarts cannot re-notify the same terminal transition. Deliberate (user-
initiated) cancels are suppressed, mirroring push_job_notice's policy.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOTICE_SUBDIR = ("inbox", "from-jobs")


def notice_path(project_dir: str, run_id: str, state: str) -> Path:
    safe_state = "".join(c if c.isalnum() or c in "-_" else "_" for c in state.lower())
    return Path(project_dir).joinpath(*NOTICE_SUBDIR) / f"{run_id}__{safe_state}.json"


def emit_job_notice(
    project_dir: str,
    run_record: dict[str, Any],
    state: str,
    category: str,
    job_id: str | None,
) -> dict[str, Any]:
    """Write one durable notice for a terminal transition. Never raises:
    a watcher failure must not break dispatch_terminal's own bookkeeping."""
    try:
        if category == "deliberate":
            return {"emitted": False, "reason": "deliberate_cancel_suppressed"}
        run_id = run_record["run_id"]
        path = notice_path(project_dir, run_id, state)
        if path.exists():
            return {"emitted": False, "reason": "duplicate", "path": str(path)}
        remote = run_record.get("remote") or {}
        notice = {
            "kind": "job_notice",
            "version": 1,
            "run_id": run_id,
            "tool": run_record.get("tool", "raw"),
            "state": state,
            "category": category,
            "job_id": job_id,
            "work_item_id": run_record.get("work_item_id"),  # present once the
            # control plane stamps runs; None for plain compchem-tools runs.
            "lifecycle": run_record.get("lifecycle"),
            "local_run_dir": remote.get("local_run_dir"),
            "remote_run_dir": remote.get("remote_run_dir"),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            # agentd-compatible event envelope (source event id is the dedupe key):
            "event": {
                "source_id": "jobs:poller",
                "source_event_id": f"{run_id}:{state}",
                "actor": "system:job-watcher",
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(notice, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic publish: readers never see a partial notice
        return {"emitted": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 — watcher must not break dispatch
        return {"emitted": False, "reason": f"error: {exc}"}
