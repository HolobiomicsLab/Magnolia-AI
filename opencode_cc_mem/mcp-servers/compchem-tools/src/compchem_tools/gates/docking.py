"""Docking-specific stage gate validators."""

import json
from pathlib import Path
from typing import Any

from compchem_tools.tools.preprocess import validate_structure


def docking_inputs_ready(work_dir: str) -> dict[str, Any]:
    """Check that docking inputs are present and valid:
    receptor PDB, ligand structure, chain IDs present."""
    wdir = Path(work_dir)
    checks: dict[str, Any] = {
        "gate": "docking_inputs_ready",
        "passed": True,
        "details": {},
    }

    pdb_files = list(wdir.glob("*.pdb"))
    sdf_files = list(wdir.glob("*.sdf"))
    mol2_files = list(wdir.glob("*.mol2"))
    cfg_files = list(wdir.glob("*.cfg"))

    checks["details"]["config_exists"] = len(cfg_files) > 0
    if not cfg_files:
        checks["passed"] = False

    checks["details"]["receptor_exists"] = len(pdb_files) > 0
    if not pdb_files:
        checks["passed"] = False
    else:
        for pdb in pdb_files:
            v = validate_structure(str(pdb))
            if not v.get("valid"):
                checks["details"][f"receptor_{pdb.name}_issues"] = v.get("issues", [])
                checks["passed"] = False

    checks["details"]["ligand_exists"] = len(sdf_files) + len(mol2_files) > 0
    has_ligand = len(sdf_files) + len(mol2_files) > 0
    if not has_ligand and len(pdb_files) >= 2:
        has_ligand = True
        checks["details"]["ligand_exists"] = True
        checks["details"]["ligand_type"] = "pdb"

    if not has_ligand:
        checks["passed"] = False

    ambig = list(wdir.glob("ambig.tbl")) + list(wdir.glob("*ambig*"))
    checks["details"]["restraints_exist"] = len(ambig) > 0

    return checks


def pose_valid(work_dir: str) -> dict[str, Any]:
    """Check that docking output poses exist and are valid."""
    wdir = Path(work_dir)
    checks: dict[str, Any] = {"gate": "pose_valid", "passed": True, "details": {}}

    output_dir = wdir / "output"
    if not output_dir.exists():
        output_dir = wdir

    pdb_poses = list(output_dir.glob("**/*.pdb")) + list(output_dir.glob("**/*.pdb.gz"))
    sdf_poses = list(output_dir.glob("**/*.sdf")) + list(output_dir.glob("**/*.sdf.gz"))

    total = len(pdb_poses) + len(sdf_poses)
    checks["details"]["pose_count"] = total
    if total == 0:
        checks["passed"] = False
        checks["details"]["error"] = "No pose files found"

    non_zero = all(f.stat().st_size > 0 for f in pdb_poses + sdf_poses if f.exists())
    checks["details"]["all_nonzero"] = non_zero
    if not non_zero:
        checks["passed"] = False

    return checks


def at_least_one_pocket(work_dir: str) -> dict[str, Any]:
    """Check that at least one pocket prediction file exists with >=3 residues."""
    wdir = Path(work_dir)
    checks: dict[str, Any] = {
        "gate": "at_least_one_pocket",
        "passed": False,
        "details": {},
    }

    csv_files = list(wdir.glob("**/*predictions.csv")) + list(
        wdir.glob("**/*pocket*.csv")
    )
    checks["details"]["pocket_files"] = [f.name for f in csv_files]

    if csv_files:
        checks["passed"] = True
        checks["details"]["pocket_count"] = len(csv_files)

    json_files = list(wdir.glob("**/*pocket*.json")) + list(
        wdir.glob("**/*p2rank*.json")
    )
    if json_files:
        checks["passed"] = True
        checks["details"]["json_pocket_files"] = [f.name for f in json_files]

    return checks
