"""GROMACS molecular dynamics tools: setup, run, and parse results."""

import re
import subprocess
from pathlib import Path
from typing import Any


def gromacs_setup(
    structure: str,
    topology: str | None = None,
    forcefield: str = "amber99sb-ildn",
    water: str = "tip3p",
    box_type: str = "dodecahedron",
    box_distance: float = 1.0,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Set up a GROMACS MD simulation: generate topology, define box, solvate,
    and add ions. Returns paths to .tpr and other setup files."""
    struct = Path(structure)
    if not struct.exists():
        return {"success": False, "error": f"Structure file not found: {structure}"}

    out = Path(output_dir) if output_dir else struct.parent / "gromacs_setup"
    out.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "success": False,
        "output_dir": str(out),
        "steps_completed": [],
    }

    try:
        # Step 1: Generate topology (pdb2gmx)
        gro_file = out / "processed.gro"
        top_file = out / "topol.top"
        cmd_pdb2gmx = [
            "gmx", "pdb2gmx",
            "-f", str(struct),
            "-o", str(gro_file),
            "-p", str(top_file),
            "-ff", forcefield,
            "-water", water,
        ]
        if topology:
            cmd_pdb2gmx.extend(["-i", str(topology)])

        proc = subprocess.run(
            cmd_pdb2gmx,
            cwd=str(out),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            result["error"] = f"pdb2gmx failed: {proc.stderr[-500:]}"
            return result
        result["steps_completed"].append("pdb2gmx")
        result["gro_file"] = str(gro_file)
        result["topol_file"] = str(top_file)

        # Step 2: Define box (editconf)
        boxed_gro = out / "boxed.gro"
        cmd_editconf = [
            "gmx", "editconf",
            "-f", str(gro_file),
            "-o", str(boxed_gro),
            "-c",
            "-d", str(box_distance),
            "-bt", box_type,
        ]
        proc = subprocess.run(
            cmd_editconf,
            cwd=str(out),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            result["error"] = f"editconf failed: {proc.stderr[-500:]}"
            return result
        result["steps_completed"].append("editconf")

        # Step 3: Solvate (solvate)
        solvated_gro = out / "solvated.gro"
        cmd_solvate = [
            "gmx", "solvate",
            "-cp", str(boxed_gro),
            "-p", str(top_file),
            "-o", str(solvated_gro),
        ]
        proc = subprocess.run(
            cmd_solvate,
            cwd=str(out),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            result["error"] = f"solvate failed: {proc.stderr[-500:]}"
            return result
        result["steps_completed"].append("solvate")

        # Step 4: Add ions (grompp + genion)
        ions_tpr = out / "ions.tpr"
        ions_mdp = out / "ions.mdp"
        ions_mdp.write_text(
            "integrator = minimization\n"
            "nsteps = 0\n"
            "emtol = 1000\n"
            "rcoulomb = 1.0\n"
            "rvdw = 1.0\n"
        )
        cmd_grompp = [
            "gmx", "grompp",
            "-f", str(ions_mdp),
            "-c", str(solvated_gro),
            "-p", str(top_file),
            "-o", str(ions_tpr),
            "--maxwarn", "2",
        ]
        proc = subprocess.run(
            cmd_grompp,
            cwd=str(out),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            result["error"] = f"grompp (ions) failed: {proc.stderr[-500:]}"
            return result
        result["steps_completed"].append("grompp_ions")

        # genion: replace solvent with ions (needs stdin for solvent group)
        ionized_gro = out / "ionized.gro"
        cmd_genion = [
            "gmx", "genion",
            "-s", str(ions_tpr),
            "-p", str(top_file),
            "-o", str(ionized_gro),
            "-pname", "NA",
            "-nname", "CL",
            "-neutral",
        ]
        proc = subprocess.run(
            cmd_genion,
            cwd=str(out),
            capture_output=True,
            text=True,
            input="SOL\n",
            timeout=60,
        )
        if proc.returncode != 0:
            result["error"] = f"genion failed: {proc.stderr[-500:]}"
            return result
        result["steps_completed"].append("genion")
        result["ionized_gro"] = str(ionized_gro)

        result["success"] = True
        return result

    except FileNotFoundError:
        return {"success": False, "error": "gmx binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "GROMACS setup timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def gromacs_run(
    tpr_file: str,
    output_dir: str | None = None,
    ncores: int = 4,
) -> dict[str, Any]:
    """Run a GROMACS MD simulation from a .tpr file.
    Returns paths to trajectory, energy, and log files."""
    tpr = Path(tpr_file)
    if not tpr.exists():
        return {"success": False, "error": f"TPR file not found: {tpr_file}"}

    out = Path(output_dir) if output_dir else tpr.parent
    out.mkdir(parents=True, exist_ok=True)

    deffnm = out / "md"
    cmd = [
        "gmx", "mdrun",
        "-s", str(tpr),
        "-deffnm", str(deffnm),
        "-nt", str(ncores),
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(out),
            capture_output=True,
            text=True,
            timeout=None,
        )

        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output_dir": str(out),
            "command": " ".join(cmd),
            "returncode": proc.returncode,
        }

        if proc.returncode == 0:
            result["trajectory"] = str(deffnm) + ".xtc" if (out / "md.xtc").exists() else None
            result["energy_file"] = str(deffnm) + ".edr" if (out / "md.edr").exists() else None
            result["log_file"] = str(deffnm) + ".log" if (out / "md.log").exists() else None
            result["final_structure"] = str(deffnm) + ".gro" if (out / "md.gro").exists() else None
        else:
            result["error"] = proc.stderr[-500:] if proc.stderr else "gmx mdrun failed"

        return result

    except FileNotFoundError:
        return {"success": False, "error": "gmx binary not found on PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def gromacs_parse(
    energy_file: str | None = None,
    trajectory: str | None = None,
) -> dict[str, Any]:
    """Parse GROMACS output files: extract energy terms from .edr and
    trajectory summary from .xtc/.trr."""
    result: dict[str, Any] = {
        "success": True,
        "energy": None,
        "trajectory": None,
    }

    if energy_file:
        efile = Path(energy_file)
        if not efile.exists():
            return {"success": False, "error": f"Energy file not found: {energy_file}"}
        result["energy"] = _parse_energy(efile)

    if trajectory:
        tfile = Path(trajectory)
        if not tfile.exists():
            return {"success": False, "error": f"Trajectory file not found: {trajectory}"}
        result["trajectory"] = _parse_trajectory_summary(tfile)

    if not energy_file and not trajectory:
        return {"success": False, "error": "Must provide at least one of: energy_file, trajectory"}

    return result


def _parse_energy(edr_file: Path) -> dict[str, Any]:
    """Extract energy terms from a GROMACS .edr file using gmx energy."""
    xvg_file = edr_file.parent / "energy.xvg"
    cmd = [
        "gmx", "energy",
        "-f", str(edr_file),
        "-o", str(xvg_file),
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(edr_file.parent),
            capture_output=True,
            text=True,
            input="Potential Kinetic-En. Total-Energy Temperature Pressure\n",
            timeout=60,
        )
        if proc.returncode != 0:
            return {"parsed": False, "error": proc.stderr[-300:] if proc.stderr else "gmx energy failed"}

        if xvg_file.exists():
            return _parse_xvg(xvg_file)
        return {"parsed": False, "error": "XVG output not generated"}

    except FileNotFoundError:
        return {"parsed": False, "error": "gmx binary not found"}
    except Exception as e:
        return {"parsed": False, "error": str(e)}


def _parse_xvg(xvg_path: Path) -> dict[str, Any]:
    """Parse an XVG file produced by gmx energy."""
    result: dict[str, Any] = {"parsed": True, "terms": {}, "frame_count": 0}
    try:
        lines = xvg_path.read_text().strip().split("\n")
        header = None
        data_rows = []
        for line in lines:
            if line.startswith("@") and "s legend" in line:
                # Parse legend: @ s0 legend "Potential"
                parts = line.split('"')
                if len(parts) >= 2:
                    if header is None:
                        header = ["time"]
                    header.append(parts[1])
            elif line.startswith("#") or line.startswith("@"):
                continue
            else:
                data_rows.append(line.split())

        if header and data_rows:
            result["frame_count"] = len(data_rows)
            for i, name in enumerate(header):
                if name == "time":
                    continue
                values = []
                for row in data_rows:
                    if i < len(row):
                        try:
                            values.append(float(row[i]))
                        except (ValueError, TypeError):
                            pass
                if values:
                    result["terms"][name] = {
                        "mean": sum(values) / len(values),
                        "min": min(values),
                        "max": max(values),
                        "last": values[-1],
                    }
    except Exception as e:
        result["parsed"] = False
        result["error"] = str(e)
    return result


def _parse_trajectory_summary(traj_file: Path) -> dict[str, Any]:
    """Get trajectory summary using gmx check or basic file info."""
    result: dict[str, Any] = {
        "file": str(traj_file),
        "format": traj_file.suffix,
        "size_bytes": traj_file.stat().st_size,
    }

    try:
        cmd = ["gmx", "check", "-f", str(traj_file)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = proc.stdout + proc.stderr
        # Try to extract frame count
        match = re.search(r"(\d+)\s+frame", output)
        if match:
            result["frame_count"] = int(match.group(1))
        result["check_output"] = output[-300:]
    except (FileNotFoundError, Exception):
        pass

    return result
