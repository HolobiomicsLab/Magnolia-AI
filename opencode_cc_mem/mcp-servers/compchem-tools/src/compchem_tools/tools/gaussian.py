"""Gaussian quantum chemistry tools: input generation, execution, and output parsing."""

import re
import subprocess
from pathlib import Path
from typing import Any


def gaussian_setup(
    input_file: str,
    method: str = "B3LYP",
    basis: str = "6-31G*",
    charge: int = 0,
    multiplicity: int = 1,
    task: str = "SP",
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Generate Gaussian .com input file from coordinates.
    Returns path to generated .com file."""
    inp = Path(input_file)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_file}"}

    work_dir = Path(output_dir) if output_dir else inp.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    # Map task to Gaussian route keyword
    task_map = {
        "SP": "SP",
        "OPT": "OPT",
        "FREQ": "FREQ",
        "OPTFREQ": "OPT FREQ",
        "TS": "OPT=TS",
        "IRC": "IRC",
    }
    task_keyword = task_map.get(task.upper(), "SP")

    # Build route line
    route_line = f"# {method}/{basis} {task_keyword}"

    # Read coordinates
    coords = inp.read_text().strip()

    # Build Gaussian .com file
    com_name = inp.stem + ".com"
    com_path = work_dir / com_name
    chk_name = inp.stem + ".chk"

    lines = [
        f"%chk={chk_name}",
        f"%nshared=4",
        f"%mem=4GB",
        route_line,
        "",
        f"Title: {inp.stem} {method}/{basis} {task}",
        "",
        f"{charge} {multiplicity}",
    ]

    # Parse coordinates from input file
    coord_lines = _extract_coordinates(coords)
    if coord_lines:
        lines.extend(coord_lines)
    else:
        lines.append(coords)

    lines.append("")
    lines.append("")

    com_path.write_text("\n".join(lines))

    return {
        "success": True,
        "input_file": str(com_path),
        "method": method,
        "basis": basis,
        "charge": charge,
        "multiplicity": multiplicity,
        "task": task,
        "route_line": route_line,
    }


def gaussian_run(
    input_file: str,
    output_dir: str | None = None,
    ncores: int = 4,
) -> dict[str, Any]:
    """Run Gaussian calculation from a .com file.
    Returns paths to output files."""
    inp = Path(input_file)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_file}"}

    work_dir = Path(output_dir) if output_dir else inp.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    log_file = inp.with_suffix(".log")

    try:
        proc = subprocess.run(
            ["g16", str(inp)],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=86400,
        )

        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output_file": str(log_file) if log_file.exists() else None,
            "returncode": proc.returncode,
        }

        if proc.returncode != 0:
            result["error"] = proc.stderr[-500:] if proc.stderr else "Gaussian calculation failed"
            result["stdout_tail"] = proc.stdout[-500:] if proc.stdout else None

        return result

    except FileNotFoundError:
        return {"success": False, "error": "g16 binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Gaussian timed out after 86400s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def gaussian_parse(output_file: str) -> dict[str, Any]:
    """Parse Gaussian .log output for energy, HOMO-LUMO, and frequencies.
    Returns parsed results as a dictionary."""
    out = Path(output_file)
    if not out.exists():
        return {"success": False, "error": f"Output file not found: {output_file}"}

    text = out.read_text()
    result: dict[str, Any] = {
        "success": True,
        "output_file": str(out),
    }

    # Parse SCF energies — look for "SCF Done:" lines
    energies = re.findall(
        r"SCF Done:\s+E\(.+?\)\s*=\s+(-?\d+\.\d+)",
        text,
    )
    if energies:
        result["energy_hartree"] = float(energies[-1])
        result["energy_ev"] = result["energy_hartree"] * 27.2114
        result["scf_energies"] = [float(e) for e in energies]

    # Check for normal termination
    result["normal_termination"] = "Normal termination" in text

    # Check for optimization convergence
    opt_converged = bool(re.search(r"Optimized Parameters\s*\n", text))
    result["optimization_converged"] = opt_converged

    # Parse HOMO and LUMO from molecular orbital section
    orbital_energies = re.findall(
        r"Alpha\s+occ\.\s+eigenvalues\s+--\s+(.*?)\n",
        text,
    )
    orbital_virt = re.findall(
        r"Alpha\s+virt\.\s+eigenvalues\s+--\s+(.*?)\n",
        text,
    )

    if orbital_energies and orbital_virt:
        all_occ = []
        for line in orbital_energies:
            all_occ.extend([float(x) for x in line.split()])
        all_virt = []
        for line in orbital_virt:
            all_virt.extend([float(x) for x in line.split()])

        if all_occ and all_virt:
            homo = all_occ[-1]
            lumo = all_virt[0]
            result["homo"] = homo
            result["lumo"] = lumo
            result["homo_lumo_gap"] = lumo - homo

    # Parse frequencies
    freq_matches = re.findall(r"Frequencies\s+--\s+(.*?)\n", text)
    if freq_matches:
        frequencies = []
        for line in freq_matches:
            frequencies.extend([float(x) for x in line.split()])
        result["frequencies"] = frequencies

        # Check for imaginary frequencies
        imaginary = [f for f in frequencies if f < 0]
        result["imaginary_frequencies"] = imaginary
        result["num_imaginary"] = len(imaginary)

    # Parse thermal corrections
    thermal_match = re.search(
        r"Zero-point correction=\s+(-?\d+\.\d+)",
        text,
    )
    if thermal_match:
        result["zero_point_correction"] = float(thermal_match.group(1))

    thermal_e_match = re.search(
        r"Thermal correction to Energy=\s+(-?\d+\.\d+)",
        text,
    )
    if thermal_e_match:
        result["thermal_correction_energy"] = float(thermal_e_match.group(1))

    return result


def _extract_coordinates(text: str) -> list[str]:
    """Extract coordinate lines from XYZ or PDB format input.
    Returns list of coordinate lines suitable for Gaussian input."""
    lines = text.strip().split("\n")
    coord_lines = []

    # Try XYZ format
    if len(lines) >= 3:
        try:
            natoms = int(lines[0].strip())
            xyz_lines = []
            for line in lines[2:2 + natoms]:
                parts = line.split()
                if len(parts) >= 4:
                    element = parts[0]
                    x, y, z = parts[1], parts[2], parts[3]
                    xyz_lines.append(f"{element:2s}  {x:>12s}  {y:>12s}  {z:>12s}")
            if len(xyz_lines) == natoms:
                return xyz_lines
        except (ValueError, IndexError):
            pass

    # Try PDB-like format — extract ATOM/HETATM lines
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("ATOM", "HETATM")):
            try:
                element = stripped[76:78].strip()
                if not element:
                    element = stripped[12:16].strip()[:2].strip()
                x = stripped[30:38].strip()
                y = stripped[38:46].strip()
                z = stripped[46:54].strip()
                coord_lines.append(f"{element:2s}  {x:>12s}  {y:>12s}  {z:>12s}")
            except (ValueError, IndexError):
                continue

    return coord_lines
