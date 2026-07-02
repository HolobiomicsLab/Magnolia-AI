"""General structure validation gates."""

import re
from pathlib import Path
from typing import Any


def pdb_has_chain_id(
    pdb_path: str, expected_chain: str | None = None
) -> dict[str, Any]:
    """Check that a PDB file has chain IDs (optionally a specific chain)."""
    p = Path(pdb_path)
    if not p.exists():
        return {"passed": False, "error": f"File not found: {pdb_path}"}

    chain_ids = set()
    for line in p.read_text().split("\n"):
        if line.startswith(("ATOM", "HETATM")) and len(line) > 21:
            cid = line[21].strip()
            if cid:
                chain_ids.add(cid)

    result: dict[str, Any] = {
        "passed": len(chain_ids) > 0,
        "chain_ids": sorted(chain_ids),
    }

    if expected_chain:
        result["passed"] = expected_chain in chain_ids
        result["expected"] = expected_chain

    return result


def file_size_nonzero(file_path: str) -> dict[str, Any]:
    """Check that a file exists and has non-zero size."""
    p = Path(file_path)
    if not p.exists():
        return {"passed": False, "error": f"File not found: {file_path}"}
    size = p.stat().st_size
    return {"passed": size > 0, "size_bytes": size, "path": str(p)}


def structure_parseable(file_path: str) -> dict[str, Any]:
    """Check that a structure file is parseable."""
    from compchem_tools.tools.preprocess import validate_structure

    result = validate_structure(file_path)
    return {"passed": result.get("valid", False), "details": result}


def qm_inputs_defined(work_dir: str) -> dict[str, Any]:
    """Check that QM inputs are properly defined: charge/multiplicity present
    and coordinate files are valid."""
    wdir = Path(work_dir)
    checks: dict[str, Any] = {
        "gate": "qm_inputs_defined",
        "passed": True,
        "details": {},
    }

    # Check for coordinate files
    xyz_files = list(wdir.glob("*.xyz"))
    pdb_files = list(wdir.glob("*.pdb"))
    coord_files = xyz_files + pdb_files

    checks["details"]["coordinate_files"] = [f.name for f in coord_files]
    if not coord_files:
        checks["passed"] = False
        checks["details"]["error"] = "No coordinate files (.xyz or .pdb) found"
        return checks

    # Validate XYZ files have content
    for xyz in xyz_files:
        lines = xyz.read_text().strip().split("\n")
        if len(lines) < 3:
            checks["passed"] = False
            checks["details"][f"{xyz.name}_valid"] = False
            checks["details"][f"{xyz.name}_error"] = "XYZ file too short"
        else:
            try:
                natoms = int(lines[0].strip())
                coord_lines = len(lines) - 2
                if coord_lines < natoms:
                    checks["passed"] = False
                    checks["details"][f"{xyz.name}_valid"] = False
                    checks["details"][f"{xyz.name}_error"] = (
                        f"Expected {natoms} atoms, found {coord_lines} coordinate lines"
                    )
                else:
                    checks["details"][f"{xyz.name}_valid"] = True
                    checks["details"][f"{xyz.name}_natoms"] = natoms
            except ValueError:
                checks["passed"] = False
                checks["details"][f"{xyz.name}_valid"] = False
                checks["details"][f"{xyz.name}_error"] = "First line is not an atom count"

    # Check for ORCA or Gaussian input files that define charge/multiplicity
    orca_inps = list(wdir.glob("*.inp"))
    gaussian_coms = list(wdir.glob("*.com"))

    has_charge_mult = False
    for inp in orca_inps + gaussian_coms:
        content = inp.read_text()
        # Look for charge multiplicity pattern (e.g., "* xyz 0 1" for ORCA,
        # or standalone "0 1" line for Gaussian)
        if "xyz" in content or "xyzfile" in content:
            if re.search(r"xyzfile?\s+(-?\d+)\s+(\d+)", content):
                has_charge_mult = True
        if re.search(r"^-?\d+\s+\d+\s*$", content, re.MULTILINE):
            has_charge_mult = True

    checks["details"]["charge_multiplicity_defined"] = has_charge_mult
    if not has_charge_mult and not orca_inps and not gaussian_coms:
        # No input files yet — charge/mult not yet defined, but coordinates exist
        checks["details"]["note"] = "No QM input files found; charge/multiplicity not yet defined"
        checks["passed"] = False

    return checks


def scf_converged(work_dir: str) -> dict[str, Any]:
    """Check that QM output files show SCF convergence."""
    wdir = Path(work_dir)
    checks: dict[str, Any] = {
        "gate": "scf_converged",
        "passed": False,
        "details": {},
    }

    # Check ORCA output files
    orca_outs = list(wdir.glob("*.out"))
    # Check Gaussian log files
    gaussian_logs = list(wdir.glob("*.log"))

    all_outputs = orca_outs + gaussian_logs
    checks["details"]["output_files"] = [f.name for f in all_outputs]

    if not all_outputs:
        checks["details"]["error"] = "No QM output files (.out or .log) found"
        return checks

    for outf in all_outputs:
        try:
            content = outf.read_text()
        except Exception as e:
            checks["details"][f"{outf.name}_error"] = str(e)
            continue

        # ORCA convergence markers
        if "SCF CONVERGED" in content or "SCF converged" in content:
            checks["details"][f"{outf.name}_scf"] = "converged"
            checks["passed"] = True
        elif "SCF NOT CONVERGED" in content:
            checks["details"][f"{outf.name}_scf"] = "not_converged"
        # Gaussian convergence markers
        elif "Converged?    Yes" in content or "Normal termination" in content:
            checks["details"][f"{outf.name}_scf"] = "converged"
            checks["passed"] = True
        elif "Converged?    No" in content and "Normal termination" not in content:
            checks["details"][f"{outf.name}_scf"] = "not_converged"
        else:
            checks["details"][f"{outf.name}_scf"] = "unknown"

    return checks
