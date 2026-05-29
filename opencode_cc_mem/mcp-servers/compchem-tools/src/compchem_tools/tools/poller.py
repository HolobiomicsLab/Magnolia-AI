"""Async job lifecycle poller for ssh-slurm jobs.

Policy on top of compchem_tools.tools.ssh_slurm primitives. Pure
composition; no new I/O primitives. See spec:
  docs/superpowers/specs/2026-05-29-hpc-azzurra-async-lifecycle-design.md
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import logging
import threading

import yaml

from compchem_memory.tiers.project import ProjectManager
from compchem_tools.tools import ssh_slurm


log = logging.getLogger(__name__)

# Re-entrancy guard: only one sweep runs at a time per process.
_SWEEP_LOCK = threading.Lock()

# Reused ProjectManager (same global_base as ssh_slurm._PROJECT_MANAGER).
_PROJECT_MANAGER = ProjectManager(global_base=Path.home() / ".magnolia")

# State categorization (sacct strings; align with ssh_slurm._RUNNING_STATES etc.)
_SCIENCE_FAILURE_STATES = {
    "FAILED", "F",
    "TIMEOUT", "TO",
    "OUT_OF_MEMORY", "OOM",
}
_INFRA_FAILURE_STATES = {
    "NODE_FAIL", "NF",
    "BOOT_FAIL", "BF",
    "PREEMPTED", "PR",
    "DEADLINE", "DL",
    "REVOKED", "RV",
}
_DELIBERATE_STATES = {"CANCELLED", "CA", "CANCELLED+"}


def _scan_active_runs(project_dir: str) -> list[dict[str, Any]]:
    """Return ssh-slurm runs in lifecycle ∈ {submitted, running} with a job_id.

    Defensive: a corrupt or non-dict YAML is skipped-and-logged. A run with
    no job_id (e.g. a 'submitting' breadcrumb) is also skipped — only
    pollable runs are returned.
    """
    runs_dir = Path(project_dir) / ".magnolia" / "runs"
    if not runs_dir.exists():
        return []
    active: list[dict[str, Any]] = []
    for f in sorted(runs_dir.glob("*.yaml")):
        if f.name == "INDEX.yaml":
            continue
        try:
            data = yaml.safe_load(f.read_text())
        except yaml.YAMLError as e:
            log.warning("poller: skip unparseable %s: %s", f.name, e)
            continue
        if not isinstance(data, dict):
            log.warning("poller: skip non-dict %s", f.name)
            continue
        remote = data.get("remote") or {}
        if remote.get("scheduler") != "ssh-slurm":
            continue
        if data.get("lifecycle") not in ("submitted", "running"):
            continue
        if not remote.get("job_id"):
            continue
        active.append(data)
    return active
