"""P2Rank pocket prediction tools: run predictor, parse CSV output."""

import csv
import io
import subprocess
from pathlib import Path
from typing import Any


def p2rank_predict(
    protein: str,
    output_dir: str | None = None,
    threads: int = 4,
) -> dict[str, Any]:
    """Run P2Rank pocket prediction on a protein structure.
    Returns ranked pocket list with scores and residue information."""
    prot = Path(protein)
    if not prot.exists():
        return {"success": False, "error": f"Protein file not found: {protein}"}

    out = Path(output_dir) if output_dir else prot.parent / "p2rank_output"
    out.mkdir(parents=True, exist_ok=True)

    cmd = [
        "p2rank",
        "predict",
        "-f", str(prot),
        "-o", str(out),
        "-threads", str(threads),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )

        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "output_dir": str(out),
            "command": " ".join(cmd),
            "returncode": proc.returncode,
        }

        if proc.returncode == 0:
            # Parse predictions CSV. P2Rank names it after the full input
            # filename, e.g. "1fbl.pdb_predictions.csv".
            csv_file = out / (prot.name + "_predictions.csv")
            # Also try common naming patterns
            if not csv_file.exists():
                csv_file = out / (prot.stem + "_predictions.csv")
            if not csv_file.exists():
                csv_file = out / "predictions.csv"
            if not csv_file.exists():
                csv_files = list(out.glob("*predictions*.csv"))
                csv_file = csv_files[0] if csv_files else None

            if csv_file and csv_file.exists():
                pockets = _parse_predictions_csv(csv_file)
                result["predictions_csv"] = str(csv_file)
                result["pockets"] = pockets
                result["pocket_count"] = len(pockets)
            else:
                result["predictions_csv"] = None
                result["pockets"] = []
                result["pocket_count"] = 0
                result["warning"] = "P2Rank completed but no predictions CSV found"

        else:
            result["error"] = proc.stderr[-500:] if proc.stderr else "P2Rank prediction failed"

        return result

    except FileNotFoundError:
        return {"success": False, "error": "p2rank binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "P2Rank timed out after 600s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _parse_predictions_csv(csv_path: Path) -> list[dict[str, Any]]:
    """Parse P2Rank predictions CSV into a list of pocket dicts.

    Expected columns include: name, rank, score, probability, center_x,
    center_y, center_z, residue_count, residues.
    """
    pockets = []
    try:
        text = csv_path.read_text()
        # P2Rank may produce comments starting with '#'
        lines = [l for l in text.split("\n") if not l.startswith("#")]
        reader = csv.DictReader(io.StringIO("\n".join(lines)))
        for row in reader:
            # P2Rank pads both column names and values with spaces
            # (e.g. "  rank", "    9.77"), so strip keys and values.
            pocket: dict[str, Any] = {
                (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()
            }
            # Convert numeric fields
            for key in ("rank", "score", "probability", "center_x", "center_y", "center_z", "residue_count", "volume"):
                if key in pocket:
                    try:
                        pocket[key] = float(pocket[key])
                        if key in ("rank", "residue_count"):
                            pocket[key] = int(pocket[key])
                    except (ValueError, TypeError):
                        pass
            # Parse residue list. P2Rank 2.5.x names this column "residue_ids";
            # older/other formats use "residues".
            residues_str = pocket.get("residue_ids") or pocket.get("residues")
            if isinstance(residues_str, str):
                pocket["residue_list"] = [
                    r.strip() for r in residues_str.split() if r.strip()
                ]
            pockets.append(pocket)
    except Exception:
        pass
    return pockets
