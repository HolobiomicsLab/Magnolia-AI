"""Run-directory hygiene gates.

These enforce the job-execution rule that a run directory must be
self-contained (real copies, not symlinks pointing elsewhere) before it is
handed to a scheduler or rsynced to a cluster. A symlink escaping the run
dir breaks reproducibility and can silently make the remote job read
different inputs than the local pre-submit gates checked.
"""

from pathlib import Path
from typing import Any


def run_dir_self_contained(work_dir: str) -> dict[str, Any]:
    """Every symlink under ``work_dir`` must resolve to a path INSIDE
    ``work_dir``. Broken links and links pointing outside both fail —
    fail-closed, per the pre-submit gate contract."""
    root = Path(work_dir).resolve()
    if not root.is_dir():
        return {"passed": False, "error": f"Working directory not found: {work_dir}"}

    violations: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError):
            violations.append(f"{path.name}: broken symlink")
            continue
        if not str(target).startswith(str(root) + "/") and target != root:
            violations.append(f"{path.name}: -> {target} (outside run dir)")

    result: dict[str, Any] = {"passed": not violations}
    if violations:
        result["violations"] = violations
        result["error"] = (
            "Run dir is not self-contained: " + "; ".join(violations[:5])
            + ("; ..." if len(violations) > 5 else "")
        )
    return result
