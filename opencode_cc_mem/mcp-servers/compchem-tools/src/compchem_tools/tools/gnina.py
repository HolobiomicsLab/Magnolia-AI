"""Gnina docking tools: classical and covalent docking, result parsing."""

import os
import subprocess
from pathlib import Path
from typing import Any


def gnina_dock(
    receptor: str,
    ligand: str,
    out_dir: str | None = None,
    autobox_ligand: str | None = None,
    center_x: float | None = None,
    center_y: float | None = None,
    center_z: float | None = None,
    size_x: float = 25.0,
    size_y: float = 25.0,
    size_z: float = 25.0,
    num_modes: int = 20,
    exhaustiveness: int = 8,
    seed: int | None = None,
    covalent: bool = False,
    covalent_receptor_atom: str | None = None,
    covalent_ligand_atom_pattern: str | None = None,
) -> dict[str, Any]:
    """Run Gnina molecular docking (classical or covalent mode).
    Returns paths to output poses and scores."""
    rec = Path(receptor)
    lig = Path(ligand)
    if not rec.exists():
        return {"success": False, "error": f"Receptor not found: {receptor}"}
    if not lig.exists():
        return {"success": False, "error": f"Ligand not found: {ligand}"}

    output = Path(out_dir) if out_dir else rec.parent / "gnina_output"
    output.mkdir(parents=True, exist_ok=True)

    out_sdf = output / "docked.sdf"
    out_log = output / "gnina.log"

    cmd = [
        "gnina",
        "-r", str(rec),
        "-l", str(lig),
        "-o", str(out_sdf),
        "--num_modes", str(num_modes),
        "--exhaustiveness", str(exhaustiveness),
        "--size_x", str(size_x),
        "--size_y", str(size_y),
        "--size_z", str(size_z),
    ]

    if autobox_ligand:
        cmd.extend(["--autobox_ligand", autobox_ligand])
    elif all(v is not None for v in (center_x, center_y, center_z)):
        cmd.extend([
            "--center_x", str(center_x),
            "--center_y", str(center_y),
            "--center_z", str(center_z),
        ])
    else:
        cmd.extend(["--autobox_ligand", str(lig)])

    if seed is not None:
        cmd.extend(["--seed", str(seed)])

    if covalent:
        if not covalent_receptor_atom or not covalent_ligand_atom_pattern:
            return {
                "success": False,
                "error": "Covalent docking requires --covalent_receptor_atom and --covalent_ligand_atom_pattern",
            }
        cmd.extend(["--covalent_rec_res", covalent_receptor_atom])
        cmd.extend(["--covalent_lig_atom_pattern", covalent_ligand_atom_pattern])

    try:
        with open(out_log, "w") as log_f:
            proc = subprocess.run(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=3600,
            )

        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output_dir": str(output),
            "poses_file": str(out_sdf) if out_sdf.exists() else None,
            "log_file": str(out_log),
            "command": " ".join(cmd),
        }

        if proc.returncode != 0:
            result["error"] = out_log.read_text()[-500:] if out_log.exists() else "gnina failed"

        return result

    except FileNotFoundError:
        return {"success": False, "error": "gnina binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "gnina timed out after 3600s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def gnina_parse_results(run_dir: str) -> dict[str, Any]:
    """Parse Gnina output SDF for docking scores and pose information."""
    base = Path(run_dir)
    sdf_files = list(base.glob("*.sdf"))
    if not sdf_files:
        sdf_files = list(base.glob("**/*.sdf"))

    if not sdf_files:
        return {"success": False, "error": "No SDF output files found", "pose_count": 0}

    results: dict[str, Any] = {
        "run_dir": str(run_dir),
        "poses": [],
        "best_affinity": None,
        "best_cnnscore": None,
        "pose_count": 0,
    }

    for sdf in sdf_files:
        text = sdf.read_text()
        poses = _parse_gnina_sdf(text)
        results["poses"].extend(poses)

    if results["poses"]:
        results["best_affinity"] = min(
            (p.get("minimizedAffinity", float("inf")) for p in results["poses"]),
            default=None,
        )
        results["best_cnnscore"] = max(
            (p.get("CNNscore", 0.0) for p in results["poses"]),
            default=None,
        )

    results["pose_count"] = len(results["poses"])
    results["success"] = len(results["poses"]) > 0
    return results


def _parse_gnina_sdf(text: str) -> list[dict[str, Any]]:
    """Parse Gnina SDF output extracting pose properties."""
    poses = []
    current: dict[str, Any] = {}

    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("> <minimizedAffinity>"):
            current["reading"] = "affinity"
        elif line.startswith("> <CNNscore>"):
            current["reading"] = "cnnscore"
        elif line.startswith("> <CNNaffinity>"):
            current["reading"] = "cnnaffinity"
        elif line.startswith("$$$$"):
            if current.get("affinity") is not None:
                poses.append({
                    "minimizedAffinity": current.get("affinity"),
                    "CNNscore": current.get("cnnscore"),
                    "CNNaffinity": current.get("cnnaffinity"),
                })
            current = {}
        elif "reading" in current:
            try:
                val = float(line)
                if current["reading"] == "affinity":
                    current["affinity"] = val
                elif current["reading"] == "cnnscore":
                    current["cnnscore"] = val
                elif current["reading"] == "cnnaffinity":
                    current["cnnaffinity"] = val
            except ValueError:
                pass
            del current["reading"]

    return poses
