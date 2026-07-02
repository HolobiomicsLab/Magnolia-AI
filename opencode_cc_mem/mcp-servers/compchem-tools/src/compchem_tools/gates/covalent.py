"""Covalent docking stage gate validators."""

from pathlib import Path
from typing import Any


def vinyl_isomers_exist(work_dir: str) -> dict[str, Any]:
    """Check that Z and E vinyl isomer files exist."""
    wdir = Path(work_dir)
    checks: dict[str, Any] = {
        "gate": "vinyl_isomers_exist",
        "passed": True,
        "details": {},
    }

    z_files = list(wdir.glob("*vinyl_Z*")) + list(wdir.glob("*vinyl*z*"))
    e_files = list(wdir.glob("*vinyl_E*")) + list(wdir.glob("*vinyl*e*"))

    # Also check for .smi files with Z/E labels
    smi_files = list(wdir.glob("*.smi"))
    if not z_files:
        z_files = [f for f in smi_files if "z" in f.name.lower()]
    if not e_files:
        e_files = [f for f in smi_files if "e" in f.name.lower()]

    checks["details"]["z_isomer"] = [f.name for f in z_files]
    checks["details"]["e_isomer"] = [f.name for f in e_files]

    if not z_files or not e_files:
        checks["passed"] = False
        checks["details"]["error"] = "Need both Z and E vinyl isomer files"

    return checks


def smarts_exactly_one_match(
    smarts: str,
    smiles: str,
) -> dict[str, Any]:
    """Check that a SMARTS pattern matches exactly one atom in the molecule."""
    checks: dict[str, Any] = {
        "gate": "smarts_exactly_one_match",
        "passed": False,
        "details": {"smarts": smarts, "smiles": smiles},
    }

    try:
        from rdkit import Chem

        mol = Chem.MolFromSmiles(smiles)
        pattern = Chem.MolFromSmarts(smarts)

        if mol is None:
            checks["details"]["error"] = "Could not parse SMILES"
            return checks
        if pattern is None:
            checks["details"]["error"] = "Could not parse SMARTS"
            return checks

        matches = mol.GetSubstructMatches(pattern)
        n_atoms = sum(len(m) for m in matches)
        checks["details"]["match_count"] = n_atoms

        if n_atoms == 1:
            checks["passed"] = True
        else:
            checks["details"]["error"] = f"Expected 1 matching atom, got {n_atoms}"

    except ImportError:
        checks["details"]["error"] = "RDKit not available for SMARTS matching"

    return checks


def docked_poses_exist(work_dir: str) -> dict[str, Any]:
    """Check that docked pose output files exist and are non-empty."""
    wdir = Path(work_dir)
    checks: dict[str, Any] = {
        "gate": "docked_poses_exist",
        "passed": False,
        "details": {},
    }

    # Gnina output patterns
    sdf_poses = list(wdir.glob("**/*docked*.sdf")) + list(wdir.glob("**/*pose*.sdf"))
    pdb_poses = list(wdir.glob("**/*docked*.pdb")) + list(wdir.glob("**/*pose*.pdb"))
    all_poses = sdf_poses + pdb_poses

    checks["details"]["pose_files"] = [f.name for f in all_poses]
    checks["details"]["pose_count"] = len(all_poses)

    if all_poses:
        non_empty = all(f.stat().st_size > 0 for f in all_poses if f.exists())
        checks["details"]["all_nonempty"] = non_empty
        checks["passed"] = non_empty
    else:
        checks["details"]["error"] = "No docked pose files found"

    return checks
