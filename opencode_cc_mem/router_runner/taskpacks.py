"""Task packs: real, executable campaign stages the always-on system can run.

A task pack is a pre-registered, narrow execution interface — never arbitrary
shell. The first pack runs the NK2R challenge funnel (generation -> filters ->
calibration -> ranked candidates). Docking stages remain gated exactly as in
nk2r_challenge/campaign/protocol.json.

PROVISIONAL: registry shape and pack interface are subject to change.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from .models import RouterError

REPO_ROOT = Path(__file__).resolve().parents[1]
NK2R_DIR = REPO_ROOT / "nk2r_challenge"
DEFAULT_RUNS_DIR = NK2R_DIR / "runs"

TASK_PACKS = {
    "nk2r-challenge-v0.1": {
        "kind": "peptide-design-funnel",
        "protocol_file": NK2R_DIR / "campaign" / "protocol.json",
        "entry": NK2R_DIR / "src" / "nk2r_funnel.py",
    },
}


def task_pack_hash(name: str) -> str:
    """Bind the protocol to the exact content of its protocol.json."""
    pack = TASK_PACKS.get(name)
    if pack is None:
        raise RouterError(f"unknown task pack: {name}")
    return hashlib.sha256(pack["protocol_file"].read_bytes()).hexdigest()


def _load_funnel():
    spec = importlib.util.spec_from_file_location(
        "nk2r_funnel", TASK_PACKS["nk2r-challenge-v0.1"]["entry"]
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_for(work_item_id: str) -> int:
    """Deterministic per-Work-Item seed (reproducible reruns)."""
    return int(hashlib.sha256(work_item_id.encode()).hexdigest()[:8], 16) % (2**31)


def _result_from_record(work_item: dict, mandate: dict, out_dir: Path) -> dict:
    record = json.loads((out_dir / "run-record.json").read_text())
    candidate_file = out_dir / "ranked_candidates.csv"
    return {
        "run_id": out_dir.name,
        "protocol": work_item["protocol"],
        "protocol_hash": task_pack_hash(work_item["protocol"]),
        "outcome": "completed" if all(record["acceptance"].values()) else "failed",
        "gate_report": {
            "acceptance": record["acceptance"],
            "calibration_passed": record["stages"]["calibrate"]["passed"],
            "note": record["stages"]["calibrate"]["note"],
        },
        "receipt": {
            "work_item_id": work_item["work_item_id"],
            "mandate_id": mandate["mandate_id"],
            "run_id": out_dir.name,
            "scope": mandate["scope"],
            "selected": record["stages"]["rank"]["selected"],
            "generator_shares": record["stages"]["rank"]["generator_shares"],
        },
        "candidate_file": str(candidate_file),
    }


def run_nk2r(work_item: dict, mandate: dict, out_root: Path | None = None, attempt: int = 1) -> dict:
    funnel = _load_funnel()
    out_root = Path(out_root) if out_root else DEFAULT_RUNS_DIR
    # Attempt-scoped directory: attempt N's cleanup can never touch attempt N+1.
    out_dir = out_root / work_item["work_item_id"] / f"attempt-{attempt}"
    funnel.run(
        seed=_seed_for(work_item["work_item_id"]),
        per_generator=120,
        target_n=60,
        out_dir=out_dir,
    )
    return _result_from_record(work_item, mandate, out_dir)


def run_nk2r_reconcile(work_item: dict, mandate: dict, attempt: int, out_root: Path | None = None) -> dict | None:
    """If the interrupted attempt's external run already produced its records,
    finalize from them instead of re-running (no duplicate execution)."""
    out_root = Path(out_root) if out_root else DEFAULT_RUNS_DIR
    out_dir = out_root / work_item["work_item_id"] / f"attempt-{attempt}"
    if not (out_dir / "run-record.json").exists():
        return None
    return _result_from_record(work_item, mandate, out_dir)


def run_task_pack(name: str, work_item: dict, mandate: dict, out_root: Path | None = None, attempt: int = 1) -> dict:
    if name == "nk2r-challenge-v0.1":
        return run_nk2r(work_item, mandate, out_root, attempt=attempt)
    raise RouterError(f"no runner for task pack: {name}")


def reconcile_task_pack(name: str, work_item: dict, mandate: dict, attempt: int, out_root: Path | None = None) -> dict | None:
    if name == "nk2r-challenge-v0.1":
        return run_nk2r_reconcile(work_item, mandate, attempt, out_root)
    raise RouterError(f"no runner for task pack: {name}")
