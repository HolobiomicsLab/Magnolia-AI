"""HADDOCK3 domain tools: run, parse, generate restraints."""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def haddock3_run(
    config_path: str,
    run_dir: str | None = None,
    ncores: int = 40,
    mode: str = "local",
    restart_from: int | None = None,
) -> dict[str, Any]:
    """Validate inputs, write config if needed, launch haddock3.
    Returns PID and run directory path."""
    cfg = Path(config_path)
    if not cfg.exists():
        return {"success": False, "error": f"Config not found: {config_path}"}

    work_dir = cfg.parent
    if run_dir:
        work_dir = Path(run_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(cfg), str(work_dir / cfg.name))
        cfg = work_dir / cfg.name

    cmd = ["haddock3", str(cfg)]
    if restart_from is not None:
        cmd.extend(["--restart", str(restart_from)])

    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = str(ncores)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return {
            "success": True,
            "pid": proc.pid,
            "run_dir": str(work_dir),
            "config": str(cfg),
            "command": " ".join(cmd),
        }
    except FileNotFoundError:
        return {"success": False, "error": "haddock3 binary not found on PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def haddock3_parse_results(run_dir: str) -> dict[str, Any]:
    """Parse caprieval/clustfcc/seletopclusts outputs.
    Returns structured metrics (best score, cluster count, LRMSD range)."""
    base = Path(run_dir) / "output"
    if not base.exists():
        base = Path(run_dir)

    results: dict[str, Any] = {
        "run_dir": str(run_dir),
        "modules_found": [],
        "best_score": None,
        "cluster_count": None,
        "lrmsd_range": None,
        "capri_data": [],
        "cluster_data": [],
    }

    for module_dir in sorted(base.iterdir()):
        if module_dir.is_dir():
            parts = module_dir.name.split("_", 1)
            if len(parts) == 2:
                idx, name = parts
                results["modules_found"].append({"index": idx, "name": name})

                if "caprieval" in name:
                    capri_data = _parse_caprieval(module_dir)
                    if capri_data:
                        results["capri_data"].append(capri_data)

                if "clustfcc" in name:
                    clust_data = _parse_clustfcc(module_dir)
                    if clust_data:
                        results["cluster_data"].append(clust_data)
                        results["cluster_count"] = clust_data.get("cluster_count", 0)

    if results["capri_data"]:
        last = results["capri_data"][-1]
        if "best_score" in last:
            results["best_score"] = last["best_score"]
        if "lrmsd_min" in last:
            results["lrmsd_range"] = [last["lrmsd_min"], last["lrmsd_max"]]

    return results


def _parse_caprieval(module_dir: Path) -> dict[str, Any] | None:
    for tsv in module_dir.glob("capri_clt.tsv"):
        try:
            lines = tsv.read_text().strip().split("\n")
            if len(lines) < 2:
                continue
            header = lines[0].split("\t")
            best = lines[1].split("\t")
            data = {}
            for i, h in enumerate(header):
                if i < len(best):
                    data[h.strip()] = best[i].strip()
            result: dict[str, Any] = {"file": str(tsv), "data": data}
            if "score" in data:
                try:
                    result["best_score"] = float(data["score"])
                except (ValueError, TypeError):
                    pass
            if "lrmsd" in data:
                try:
                    result["lrmsd_min"] = float(data["lrmsd"])
                    result["lrmsd_max"] = float(data["lrmsd"])
                except (ValueError, TypeError):
                    pass
            return result
        except Exception:
            continue
    return None


def _parse_clustfcc(module_dir: Path) -> dict[str, Any] | None:
    summary = module_dir / "clustfcc.txt"
    if summary.exists():
        try:
            text = summary.read_text()
            count = 0
            for line in text.split("\n"):
                if line.strip().startswith("Cluster"):
                    count += 1
            return {"file": str(summary), "cluster_count": count}
        except Exception:
            pass
    return None


def generate_restraints(
    actpass_file_1: str,
    actpass_file_2: str,
    output_path: str,
    segid_one: str | None = None,
    segid_two: str | None = None,
) -> dict[str, Any]:
    """From actpass files, run haddock3-restraints active_passive_to_ambig.
    Returns path to ambig.tbl."""
    for f in [actpass_file_1, actpass_file_2]:
        if not Path(f).exists():
            return {"success": False, "error": f"Actpass file not found: {f}"}
        lines = Path(f).read_text().strip().split("\n")
        if len(lines) != 2:
            return {
                "success": False,
                "error": f"{f}: actpass file must have exactly 2 lines (got {len(lines)})",
            }

    cmd = [
        "haddock3-restraints",
        "active_passive_to_ambig",
        actpass_file_1,
        actpass_file_2,
    ]
    if segid_one:
        cmd.extend(["--segid-one", segid_one])
    if segid_two:
        cmd.extend(["--segid-two", segid_two])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return {"success": False, "error": result.stderr, "command": " ".join(cmd)}

        Path(output_path).write_text(result.stdout)
        return {
            "success": True,
            "output_path": str(output_path),
            "restraint_count": result.stdout.count("assign"),
            "command": " ".join(cmd),
        }
    except FileNotFoundError:
        return {"success": False, "error": "haddock3-restraints not found on PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_acpype(
    input_file: str,
    charge_method: str = "bcc",
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Run ACPYPE for ligand parameterisation. Post-process atom types to uppercase.
    Returns paths to .top and .par files."""
    inp = Path(input_file)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_file}"}

    out = Path(output_dir) if output_dir else inp.parent / f"acpype_{inp.stem}"
    out.mkdir(parents=True, exist_ok=True)

    cmd = ["acpype", "-i", str(inp), "-c", charge_method, "--cns", "-o", str(out)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return {"success": False, "error": result.stderr, "command": " ".join(cmd)}

        top_files = list(out.glob("**/*.top")) + list(out.glob("**/*CNS.top"))
        par_files = list(out.glob("**/*.par")) + list(out.glob("**/*CNS.par"))

        for par in par_files:
            _uppercase_atom_types(par)

        return {
            "success": True,
            "top_files": [str(f) for f in top_files],
            "par_files": [str(f) for f in par_files],
            "output_dir": str(out),
            "command": " ".join(cmd),
        }
    except FileNotFoundError:
        return {"success": False, "error": "acpype not found on PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _uppercase_atom_types(par_path: Path) -> None:
    text = par_path.read_text()
    lines = text.split("\n")
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("!") and not stripped.startswith("*"):
            parts = line.split()
            if len(parts) >= 6:
                atom_type = parts[5]
                if atom_type.endswith("_") and atom_type[0].islower():
                    parts[5] = atom_type.upper()
                new_lines.append("  ".join(parts))
                continue
        new_lines.append(line)
    par_path.write_text("\n".join(new_lines))
