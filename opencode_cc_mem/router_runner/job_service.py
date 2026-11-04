"""Job-service adapter (fake backend) for the authorized cancel/resubmit flow.

Boundary (user decision 2026-11-03): every job has its own run directory, and
cleanup is LIMITED TO THAT DIRECTORY — resolved from the job record, never
from worker-supplied paths, canonicalized, required to sit under the service
root and to carry the .magnolia-run marker. Cancel is refused on terminal jobs
(their results must be fetched, not deleted).

The fake backend models just enough of a cluster for the control-plane flow:
submit/status/cancel/cleanup. A real adapter would map these onto ssh-slurm
with the same guards and fixed argv.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import RouterError

TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})
MARKER = ".magnolia-run"


class FakeJobService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.jobs: dict[str, dict] = {}

    def submit(self, run_id: str, spec: dict | None = None) -> dict:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / MARKER).write_text(run_id)
        job = {
            "run_id": run_id,
            "job_id": f"job-{run_id}",
            "state": "submitted",
            "run_dir": str(run_dir),
            "spec": spec or {},
        }
        self.jobs[run_id] = job
        return job

    def status(self, run_id: str) -> str:
        return self._get(run_id)["state"]

    def set_state(self, run_id: str, state: str) -> None:
        """Test/demo helper: simulate the cluster advancing a job."""
        self._get(run_id)["state"] = state

    def cancel(self, run_id: str) -> dict:
        job = self._get(run_id)
        if job["state"] in TERMINAL_JOB_STATES:
            raise RouterError(
                f"cannot cancel {run_id}: already {job['state']} — fetch instead"
            )
        job["state"] = "cancelled"
        return job

    def cleanup(self, run_id: str) -> str:
        """Delete ONLY the recorded run dir, after all guards pass."""
        job = self._get(run_id)
        run_dir = Path(job["run_dir"]).resolve()
        if self.root not in run_dir.parents:
            raise RouterError(f"cleanup refused: {run_dir} escapes the service root")
        if run_dir == self.root:
            raise RouterError("cleanup refused: target is the service root itself")
        if not (run_dir / MARKER).is_file():
            raise RouterError(f"cleanup refused: no {MARKER} marker in {run_dir}")
        shutil.rmtree(run_dir)
        return str(run_dir)

    def _get(self, run_id: str) -> dict:
        if run_id not in self.jobs:
            raise RouterError(f"unknown job: {run_id}")
        return self.jobs[run_id]
