"""Cross-process identity tokens shared by campaign and memory services."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def verified_process_start_token(pid: int) -> str | None:
    """Return a platform-stable token for one verified process start."""
    try:
        body = Path(f"/proc/{pid}/stat").read_bytes()
    except FileNotFoundError:
        pass
    except OSError:
        return None
    else:
        fields = _proc_stat_fields(body)
        if fields is None or len(fields) <= 19:
            return None
        start_time = fields[19]
        if not start_time.isdigit():
            return None
        return f"proc:{pid}:{start_time.decode('ascii')}"
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env={"LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = " ".join(result.stdout.split())
    if result.returncode != 0 or not started or not _valid_ps_start(started):
        return None
    return f"ps:{pid}:{started}"


def _proc_stat_fields(body: bytes) -> list[bytes] | None:
    parts = body.rsplit(b")", 1)
    if len(parts) != 2:
        return None
    fields = parts[1].split()
    return fields or None


def _valid_ps_start(started: str) -> bool:
    weekdays = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    months = {
        name: index
        for index, name in enumerate(
            (
                "Jan",
                "Feb",
                "Mar",
                "Apr",
                "May",
                "Jun",
                "Jul",
                "Aug",
                "Sep",
                "Oct",
                "Nov",
                "Dec",
            ),
            start=1,
        )
    }
    parts = started.split()
    if len(parts) != 5 or parts[0] not in weekdays or parts[1] not in months:
        return False
    try:
        hour, minute, second = (int(value) for value in parts[3].split(":"))
        datetime(
            int(parts[4]),
            months[parts[1]],
            int(parts[2]),
            hour,
            minute,
            second,
            tzinfo=timezone.utc,
        )
    except (TypeError, ValueError):
        return False
    return True
