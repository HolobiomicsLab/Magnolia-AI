"""Post-run assessment: technical checks plus per-tool analysis.

The generic assessor validates the run directory and exit code. Tool-specific
analyzers (see TOOL_ANALYZERS) add domain metrics and quality flags. Unknown
tools get generic output checks only, so local scripts can be assessed too.
"""

from pathlib import Path
from typing import Any, Callable

from compchem_tools.analysis.haddock3 import analyze as analyze_haddock3

# Registry of tool-specific analyzers. Each takes a run directory Path and
# returns {"metrics": dict, "quality_flags": list[str]}. Add new tools here
# instead of growing the generic assessor.
TOOL_ANALYZERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "haddock3": analyze_haddock3,
}


def assess_run(run_dir: str, tool: str, exit_code: int = 0) -> dict[str, Any]:
    run_path = Path(run_dir)

    technical = _technical_check(run_path, tool)
    assessment: dict[str, Any] = {
        "run_dir": str(run_path),
        "tool": tool,
        "exit_code": exit_code,
        "technical": technical,
        "metrics": {},
        "quality_flags": [],
    }

    analyzer = TOOL_ANALYZERS.get(tool)
    if analyzer is not None:
        result = analyzer(run_path)
        assessment["metrics"] = result.get("metrics", {})
        assessment["quality_flags"] = result.get("quality_flags", [])

    overall = "pass"
    if exit_code != 0 or not technical["outputs_exist"]:
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
