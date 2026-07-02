"""ORCA quantum chemistry tools: input generation, execution, and output parsing."""

import re
import subprocess
from pathlib import Path
from typing import Any


def orca_setup(
    input_file: str,
    method: str = "B3LYP",
    basis: str = "def2-SVP",
    charge: int = 0,
    multiplicity: int = 1,
    task: str = "SP",
    solvent: str | None = None,
    output_dir: str | None = None,
    ncores: int = 4,
) -> dict[str, Any]:
    """Generate ORCA input file from coordinates.
    Returns path to generated .inp file."""
    inp = Path(input_file)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_file}"}

    work_dir = Path(output_dir) if output_dir else inp.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    # Map task to ORCA keyword
    task_map = {
        "SP": "",
        "OPT": "OPT",
        "FREQ": "FREQ",
        "OPTFREQ": "OPT FREQ",
        "NUMGRAD": "NUMGRAD",
    }
    task_keyword = task_map.get(task.upper(), "")

    # Build input lines
    keywords = [method, basis, task_keyword]
    if solvent:
        keywords.append(f"CPCM({solvent})")

    keyword_line = " ".join(kw for kw in keywords if kw)

    # Read coordinates from input file
    coords = inp.read_text().strip()

    # Build ORCA input file
    inp_name = inp.stem + ".inp"
    inp_path = work_dir / inp_name

    lines = [
        f"! {keyword_line}",
        f"%maxcore {1024}",
        f"%pal nprocs {ncores} end",
        f"%scf",
        f"   MaxIter 200",
        f"   ConvTol 1e-8",
        f"end",
        f"* xyzfile {charge} {multiplicity} {inp.name}",
        f"",
    ]

    inp_path.write_text("\n".join(lines))

    return {
        "success": True,
        "input_file": str(inp_path),
        "method": method,
        "basis": basis,
        "charge": charge,
        "multiplicity": multiplicity,
        "task": task,
        "solvent": solvent,
        "ncores": ncores,
    }


def orca_run(
    input_file: str,
    output_dir: str | None = None,
    ncores: int = 4,
) -> dict[str, Any]:
    """Run ORCA calculation from an .inp file.
    Returns paths to output files."""
    inp = Path(input_file)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_file}"}

    work_dir = Path(output_dir) if output_dir else inp.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            ["orca", str(inp)],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=86400,
        )

        out_file = inp.with_suffix(".out")
        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output_file": str(out_file) if out_file.exists() else None,
            "returncode": proc.returncode,
        }

        if proc.returncode != 0:
            result["error"] = proc.stderr[-500:] if proc.stderr else "ORCA calculation failed"
            result["stdout_tail"] = proc.stdout[-500:] if proc.stdout else None

        return result

    except FileNotFoundError:
        return {"success": False, "error": "orca binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "ORCA timed out after 86400s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def orca_parse(output_file: str) -> dict[str, Any]:
    """Parse ORCA output for energy, HOMO-LUMO gap, and converged geometry.
    Returns parsed results as a dictionary."""
    out = Path(output_file)
    if not out.exists():
        return {"success": False, "error": f"Output file not found: {output_file}"}

    text = out.read_text()
    result: dict[str, Any] = {
        "success": True,
        "output_file": str(out),
    }

    # Parse final single-point energy
    energies = re.findall(
        r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)",
        text,
    )
    if energies:
        result["energy_hartree"] = float(energies[-1])
        result["energy_ev"] = result["energy_hartree"] * 27.2114

    # Parse HOMO-LUMO gap
    gap_match = re.search(
        r"HOMO - LUMO gap\s+(-?\d+\.\d+)\s+(?:a\.u\.|Hartree|eV)",
        text,
    )
    if gap_match:
        result["homo_lumo_gap"] = float(gap_match.group(1))

    # Also try the ORCA 5.x format with eV explicitly
    if "homo_lumo_gap" not in result:
        gap_match_ev = re.search(
            r"HOMO-LUMO GAP\s+.*?(-?\d+\.\d+)\s+eV",
            text,
            re.IGNORECASE,
        )
        if gap_match_ev:
            result["homo_lumo_gap"] = float(gap_match_ev.group(1))

    # Check for geometry convergence
    converged_patterns = [
        r"THE OPTIMIZATION HAS CONVERGED",
        r"Geometry convergence criteria fulfilled",
    ]
    result["geometry_converged"] = any(
        re.search(p, text) for p in converged_patterns
    )

    # Check for SCF convergence
    scf_converged = "SCF CONVERGED" in text or "SCF converged" in text
    result["scf_converged"] = scf_converged

    # Parse orbital energies
    orbital_section = re.findall(
        r"ORBITAL ENERGIES\n.*?\n(.*?)(?:\n\n|\n\s*\*\*)",
        text,
        re.DOTALL,
    )
    if orbital_section:
        orbitals = []
        for line in orbital_section[0].strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    orbitals.append({
                        "occupation": float(parts[0]),
                        "energy_hartree": float(parts[1]),
                    })
                except ValueError:
                    pass
        if orbitals:
            result["orbital_energies"] = orbitals

    return result
