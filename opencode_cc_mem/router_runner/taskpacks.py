"""Task packs: real, executable stages the always-on system can run.

A task pack is a pre-registered narrow execution interface — never arbitrary
shell. The core knows a pack only by its id, kind, protocol hash and adapter;
manifests live in router_runner/taskpacks/ and are loaded by taskpack_registry.py.

PROVISIONAL: the registry shape is subject to change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .models import RouterError
from .taskpack_registry import TaskPackManifest, default_registry

DREAMING_PROTOCOL = "memory-dreaming-v0.1"
DEFAULT_OUT_ROOT = Path("/tmp/agentd-runs")
REPO_ROOT = Path(__file__).resolve().parents[1]


def task_pack_hash(name: str) -> str:
    """Bind the protocol to the exact content of its protocol file."""
    pack = default_registry().get(name)
    if pack is None:
        raise RouterError(f"unknown task pack: {name}")
    return pack.protocol_hash


def _load_dreaming():
    """Import the memory-server dreaming module.

    compchem_memory is a separate uv project under mcp-servers/; when it is not
    already importable (e.g. the router test suite), add its src/ to sys.path.
    """
    try:
        from compchem_memory import dreaming
        return dreaming
    except ImportError:
        src = REPO_ROOT / "mcp-servers" / "compchem-memory" / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from compchem_memory import dreaming
        return dreaming


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


def _result_from_dreaming_record(work_item: dict, mandate: dict, out_dir: Path) -> dict:
    record = json.loads((out_dir / "run-record.json").read_text())
    summary = record["summary"]
    acceptance = summary["acceptance"]
    verdicts = summary["verdicts"]
    return {
        "run_id": out_dir.name,
        "protocol": work_item["protocol"],
        "protocol_hash": task_pack_hash(work_item["protocol"]),
        "outcome": "completed" if all(acceptance.values()) else "failed",
        "gate_report": {
            "acceptance": acceptance,
            "checked": summary["checked"],
            "verdicts": verdicts,
            "applied": summary["applied"],
        },
        "receipt": {
            "work_item_id": work_item["work_item_id"],
            "mandate_id": mandate["mandate_id"],
            "run_id": out_dir.name,
            "scope": mandate["scope"],
            "checked": summary["checked"],
            "confirmed": verdicts.get("confirmed", 0),
            "stale": verdicts.get("stale", 0),
            "corrected": verdicts.get("wrong", 0),
            "unverifiable": verdicts.get("unverifiable", 0),
            "mutations": summary["applied"],
            "proposal_path": summary["proposal_path"],
            "log_path": summary["log_path"],
        },
        "candidate_file": str(out_dir / "dreaming-report.md"),
    }


def run_dreaming(
    work_item: dict, mandate: dict, out_root: Path | None = None, attempt: int = 1,
    protocol_file: Path | None = None,
) -> dict:
    """Execute one bounded dreaming pass against the project memory store.

    The project directory is resolved from out_root (out_root = <project>/runs),
    and the pass fails closed when that directory has no .magnolia store — a
    dreaming run must never create or guess a memory store.
    """
    if protocol_file is None:
        pack = default_registry().get(work_item["protocol"])
        if pack is None or pack.manifest.protocol_file is None:
            raise RouterError("dreaming requires the protocol file of its task-pack manifest")
        protocol_file = pack.manifest.protocol_file
    out_root = Path(out_root) if out_root else DEFAULT_OUT_ROOT
    out_dir = out_root / work_item["work_item_id"] / f"attempt-{attempt}"
    out_dir.mkdir(parents=True, exist_ok=True)

    project_dir = Path(out_root).parent
    if not (project_dir / ".magnolia").is_dir():
        raise RouterError(
            f"dreaming requires a project directory containing .magnolia; got {project_dir}"
        )

    dreaming = _load_dreaming()
    config = dreaming.DreamingConfig.from_protocol(json.loads(Path(protocol_file).read_text()))
    summary = dreaming.run_dreaming(project_dir, config=config)

    (out_dir / "run-record.json").write_text(
        json.dumps({"summary": summary, "protocol": DREAMING_PROTOCOL}, indent=2)
    )
    (out_dir / "dreaming-report.md").write_text(dreaming.render_dreaming_report(summary))
    return _result_from_dreaming_record(work_item, mandate, out_dir)


def run_dreaming_reconcile(
    work_item: dict, mandate: dict, attempt: int, out_root: Path | None = None,
) -> dict | None:
    """Finalize from an existing run record; never re-execute a finished pass."""
    out_root = Path(out_root) if out_root else DEFAULT_OUT_ROOT
    out_dir = out_root / work_item["work_item_id"] / f"attempt-{attempt}"
    if not (out_dir / "run-record.json").exists():
        return None
    return _result_from_dreaming_record(work_item, mandate, out_dir)


class DemoTaskPack:
    """A generic task pack with a real run/reconcile path and no project."""

    def __init__(self, manifest: TaskPackManifest) -> None:
        self.manifest = manifest

    def run(
        self, work_item: dict, mandate: dict, out_root: Path | None, attempt: int = 1,
    ) -> dict:
        out_root = Path(out_root) if out_root else DEFAULT_OUT_ROOT
        out_dir = out_root / work_item["work_item_id"] / f"attempt-{attempt}"
        out_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "acceptance": {"demo_completed": True},
            "stages": {
                "calibrate": {"passed": True, "note": "demo"},
                "rank": {"selected": 3, "generator_shares": {"demo": 1.0}},
            },
        }
        (out_dir / "run-record.json").write_text(json.dumps(record, indent=2))
        (out_dir / "ranked_candidates.csv").write_text(
            "id,score\ndemo-1,1\ndemo-2,2\ndemo-3,3\n"
        )
        return _result_from_record(work_item, mandate, out_dir)

    def reconcile(
        self, work_item: dict, mandate: dict, attempt: int, out_root: Path | None,
    ) -> dict | None:
        out_root = Path(out_root) if out_root else DEFAULT_OUT_ROOT
        out_dir = out_root / work_item["work_item_id"] / f"attempt-{attempt}"
        if not (out_dir / "run-record.json").exists():
            return None
        return _result_from_record(work_item, mandate, out_dir)


class DreamingTaskPack:
    def __init__(self, manifest: TaskPackManifest) -> None:
        self.manifest = manifest

    def run(
        self, work_item: dict, mandate: dict, out_root: Path | None, attempt: int = 1,
    ) -> dict:
        return run_dreaming(
            work_item, mandate, out_root, attempt=attempt, protocol_file=self.manifest.protocol_file,
        )

    def reconcile(
        self, work_item: dict, mandate: dict, attempt: int, out_root: Path | None,
    ) -> dict | None:
        return run_dreaming_reconcile(work_item, mandate, attempt, out_root)


def run_task_pack(
    name: str, work_item: dict, mandate: dict, out_root: Path | None = None, attempt: int = 1,
) -> dict:
    pack = default_registry().get(name)
    if pack is None:
        raise RouterError(f"unknown task pack: {name}")
    if pack.adapter is None:
        raise RouterError(f"task pack is retired: {name}")
    return pack.adapter.run(work_item, mandate, out_root, attempt=attempt)


def reconcile_task_pack(
    name: str, work_item: dict, mandate: dict, attempt: int, out_root: Path | None = None,
) -> dict | None:
    pack = default_registry().get(name)
    if pack is None:
        raise RouterError(f"unknown task pack: {name}")
    if pack.adapter is None:
        raise RouterError(f"task pack is retired: {name}")
    return pack.adapter.reconcile(work_item, mandate, attempt, out_root)
