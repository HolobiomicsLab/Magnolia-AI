"""xTB tools: GFN2-xTB geometry optimization and single-point calculations."""

import subprocess
from pathlib import Path
from typing import Any


def xtb_optimize(
    input_file: str,
    output_dir: str | None = None,
    method: str = "gfn2",
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
    cycles: int = 200,
    ncores: int = 4,
) -> dict[str, Any]:
    """Run xTB geometry optimization (GFN2-xTB by default).
    Returns paths to optimized structure and energy."""
    inp = Path(input_file)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_file}"}

    work_dir = Path(output_dir) if output_dir else inp.parent / "xtb_opt"
    work_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "xtb",
        str(inp),
        "--opt",
        "--gfn", method.replace("gfn", ""),
        "--chrg", str(charge),
        "--uhf", str(uhf),
        "--cycles", str(cycles),
        "--parallel", str(ncores),
    ]
    if solvent:
        cmd.extend(["--alpb", solvent])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=3600,
        )

        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output_dir": str(work_dir),
            "command": " ".join(cmd),
            "returncode": proc.returncode,
        }

        # Parse energy from output
        if proc.returncode == 0:
            energy = _parse_xtb_energy(proc.stdout)
            result["energy_hartree"] = energy
            result["energy_ev"] = energy * 27.2114 if energy else None

            opt_xyz = work_dir / "xtbopt.xyz"
            if opt_xyz.exists():
                result["optimized_structure"] = str(opt_xyz)

        else:
            result["error"] = proc.stderr[-500:] if proc.stderr else "xTB optimization failed"
            result["stdout_tail"] = proc.stdout[-500:] if proc.stdout else None

        return result

    except FileNotFoundError:
        return {"success": False, "error": "xtb binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "xTB timed out after 3600s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def xtb_singlepoint(
    input_file: str,
    output_dir: str | None = None,
    method: str = "gfn2",
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
    ncores: int = 4,
) -> dict[str, Any]:
    """Run xTB single-point energy calculation.
    Returns energy and molecular properties."""
    inp = Path(input_file)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_file}"}

    work_dir = Path(output_dir) if output_dir else inp.parent / "xtb_sp"
    work_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "xtb",
        str(inp),
        "--gfn", method.replace("gfn", ""),
        "--chrg", str(charge),
        "--uhf", str(uhf),
        "--parallel", str(ncores),
    ]
    if solvent:
        cmd.extend(["--alpb", solvent])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=600,
        )

        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output_dir": str(work_dir),
            "command": " ".join(cmd),
        }

        if proc.returncode == 0:
            energy = _parse_xtb_energy(proc.stdout)
            result["energy_hartree"] = energy
            result["energy_ev"] = energy * 27.2114 if energy else None
            result["homo_lumo_gap"] = _parse_homo_lumo(proc.stdout)

        else:
            result["error"] = proc.stderr[-500:] if proc.stderr else "xTB failed"

        return result

    except FileNotFoundError:
        return {"success": False, "error": "xtb binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "xTB timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _parse_xtb_energy(output: str) -> float | None:
    """Parse total energy from xTB output."""
    for line in output.split("\n"):
        if "TOTAL ENERGY" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "TOTAL" and i + 2 < len(parts):
                    try:
                        return float(parts[i + 2])
                    except (ValueError, IndexError):
                        pass
    return None


def _parse_homo_lumo(output: str) -> float | None:
    """Parse HOMO-LUMO gap from xTB output."""
    for line in output.split("\n"):
        if "HOMO-LUMO GAP" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                try:
                    return float(p)
                except ValueError:
                    continue
    return None
