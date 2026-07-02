"""Post-run assessment: technical checks, metric extraction, quality heuristics."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def assess_run(
    run_dir: str,
    tool: str,
    exit_code: int = 0,
) -> dict[str, Any]:
    run_path = Path(run_dir)

    assessment: dict[str, Any] = {
        "run_dir": str(run_path),
        "tool": tool,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code,
        "technical": _technical_check(run_path, tool),
        "metrics": {},
        "quality_flags": [],
    }

    assessment["metrics"] = _extract_metrics(run_path, tool)
    assessment["quality_flags"] = _quality_heuristics(assessment["metrics"], tool)

    overall = "pass"
    if not assessment["technical"]["outputs_exist"]:
        overall = "fail"
    elif assessment["quality_flags"]:
        overall = "warning"
    assessment["overall"] = overall

    return assessment


def _technical_check(run_path: Path, tool: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_dir_exists": run_path.exists(),
        "outputs_exist": False,
        "output_files": [],
        "missing_expected": [],
    }

    if not run_path.exists():
        return result

    expected_patterns = _expected_outputs(tool)
    found = []
    missing = []
    for pattern in expected_patterns:
        matches = list(run_path.glob(pattern))
        if not matches:
            matches = list(run_path.glob(f"**/{pattern}"))
        if matches:
            found.extend([str(m.relative_to(run_path)) for m in matches[:5]])
        else:
            missing.append(pattern)

    result["output_files"] = found
    result["outputs_exist"] = len(missing) == 0 or len(found) > 0
    result["missing_expected"] = missing
    return result


def _expected_outputs(tool: str) -> list[str]:
    tool_patterns = {
        "haddock3": [
            "output/*/io.json",
            "output/*_caprieval/capri_ss.tsv",
            "output/*_clustfcc/clustfcc.txt",
        ],
        "gnina": [
            "docked.pdb",
            "docked.sdf.gz",
            "*.pdb",
        ],
        "xtb": [
            "xtbopt.sdf",
            "xtbopt.xyz",
            "energy",
        ],
    }
    return tool_patterns.get(tool, ["*"])


def _extract_metrics(run_path: Path, tool: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}

    if tool == "haddock3":
        metrics.update(_extract_haddock3_metrics(run_path))
    elif tool == "gnina":
        metrics.update(_extract_gnina_metrics(run_path))

    return metrics


def _extract_haddock3_metrics(run_path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    capri_files = list(run_path.glob("output/*_caprieval/capri_clt.tsv"))
    if not capri_files:
        capri_files = list(run_path.glob("**/*_caprieval/capri_clt.tsv"))

    if capri_files:
        last_capri = sorted(capri_files)[-1]
        try:
            lines = last_capri.read_text().strip().split("\n")
            if len(lines) >= 2:
                header = lines[0].split("\t")
                best = lines[1].split("\t")
                for i, h in enumerate(header):
                    if i < len(best):
                        metrics[h.strip()] = best[i].strip()
        except Exception:
            pass

    clust_files = list(run_path.glob("output/*_clustfcc/clustfcc.txt"))
    if not clust_files:
        clust_files = list(run_path.glob("**/*_clustfcc/clustfcc.txt"))
    if clust_files:
        try:
            text = clust_files[-1].read_text()
            metrics["cluster_count"] = text.count("cluster")
        except Exception:
            pass

    return metrics


def _extract_gnina_metrics(run_path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    pdb_files = list(run_path.glob("docked*.pdb")) + list(run_path.glob("*.pdb"))
    metrics["pose_count"] = len(pdb_files)
    return metrics


def _quality_heuristics(metrics: dict[str, Any], tool: str) -> list[str]:
    flags = []

    if tool == "haddock3":
        score = metrics.get("score", metrics.get("best_score"))
        if score is not None:
            try:
                if float(score) > 0:
                    flags.append("positive_haddock_score")
            except (ValueError, TypeError):
                pass

        cluster_count = metrics.get("cluster_count")
        if cluster_count is not None and cluster_count == 0:
            flags.append("no_clusters")

        fnat = metrics.get("fnat")
        if fnat is not None:
            try:
                if float(fnat) < 0.1:
                    flags.append("very_low_fnat")
            except (ValueError, TypeError):
                pass

    return flags
