import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.lease import (  # noqa: E402
    AttemptLease,
    StaleLeaseError,
    list_leases,
    read_lease,
    stop_active_leases,
)
from campaign_runner.state import CampaignBusyError  # noqa: E402


def test_lease_is_exclusive_and_reusable(tmp_path):
    first = AttemptLease.acquire(tmp_path, "stage", 1)
    assert read_lease(tmp_path, "stage", 1)["owner_pid"] == os.getpid()
    with pytest.raises(CampaignBusyError, match="held by pid"):
        AttemptLease.acquire(tmp_path, "stage", 1)
    first.release()
    second = AttemptLease.acquire(tmp_path, "stage", 1)
    second.release()
    assert list_leases(tmp_path) == []


def test_stale_lease_requires_human(tmp_path):
    path = tmp_path / "control" / "leases" / "stage-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "stage": "stage",
                "attempt": 1,
                "owner_pid": 99999999,
                "owner_pgid": 99999999,
                "hostname": os.uname().nodename,
                "started_at": "2026-01-01T00:00:00+00:00",
                "heartbeat_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    with pytest.raises(StaleLeaseError, match="stale"):
        AttemptLease.acquire(tmp_path, "stage", 1)


def test_stop_active_leases_kills_child_of_current_owner(tmp_path):
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    path = tmp_path / "control" / "leases" / "stage-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "stage": "stage",
                "attempt": 1,
                "owner_pid": os.getpid(),
                "owner_pgid": os.getpgid(0),
                "worker_pid": process.pid,
                "worker_pgid": process.pid,
                "hostname": os.uname().nodename,
                "started_at": "2026-01-01T00:00:00+00:00",
                "heartbeat_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    errors = stop_active_leases(tmp_path)
    assert errors == []
    process.wait(timeout=5)
    assert process.poll() is not None


def test_stop_active_leases_kills_process_group(tmp_path):
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    path = tmp_path / "control" / "leases" / "stage-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "stage": "stage",
                "attempt": 1,
                "owner_pid": process.pid,
                "owner_pgid": process.pid,
                "worker_pid": process.pid,
                "worker_pgid": process.pid,
                "hostname": os.uname().nodename,
                "started_at": "2026-01-01T00:00:00+00:00",
                "heartbeat_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    errors = stop_active_leases(tmp_path)
    assert errors == []
    process.wait(timeout=5)
    assert process.poll() is not None
    assert read_lease(tmp_path, "stage", 1) is None
    time.sleep(0.05)
