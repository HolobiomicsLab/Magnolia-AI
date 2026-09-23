"""Canonical run-id minting — ONE implementation for every writer.

A run has exactly one id across its whole lifecycle: the ``submit_job`` result
recorded in the session log, the run record YAML, and any later assessment or
receipt. Keeping the mint in one dependency-free module (both the tools'
submit paths and the memory-side CLI import it) is what makes the id uniform;
three private copies is how it drifted before (P1 "uniform run_id" item).

Format: ``<tool>_<YYYYMMDD_HHMMSS>_<6hex>`` in UTC.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def generate_run_id(tool: str | None = None) -> str:
    """Return a unique ``<tool>_<YYYYMMDD_HHMMSS>_<6hex>`` id (UTC).

    The 6-hex suffix disambiguates parallel submissions that land in the same
    second. Without it, concurrent submit_job calls produce identical run_ids,
    colliding on the run record and (for ssh-slurm) the remote directory.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{tool or 'job'}_{ts}_{suffix}"
