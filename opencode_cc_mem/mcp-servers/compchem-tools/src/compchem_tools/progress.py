"""Progress parsing for HADDOCK3 runs."""

import json
from pathlib import Path
from typing import Any


def parse_haddock_progress(run_dir: str) -> dict[str, Any]:
    rdir = Path(run_dir)
    output_dir = rdir / "output"

    result: dict[str, Any] = {
        "run_dir": str(rdir),
        "completed": False,
        "modules_done": 0,
        "current_module": None,
        "total_modules": 0,
        "percent": 0.0,
        "scores": {},
        "log_tail": [],
    }

    if not output_dir.exists():
        return result

    module_dirs = sorted(
        [d for d in output_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )
    result["total_modules"] = len(module_dirs)

    completed = 0
    current = None
    for md in module_dirs:
        io_json = md / "io.json"
        if io_json.exists():
            try:
                data = json.loads(io_json.read_text())
                if data.get("finished"):
                    completed += 1
                else:
                    if current is None:
                        current = md.name
            except (json.JSONDecodeError, OSError):
                if current is None:
                    current = md.name
        else:
            if current is None:
                current = md.name

    result["modules_done"] = completed
    result["current_module"] = current

    if result["total_modules"] > 0:
        result["percent"] = round(completed / result["total_modules"] * 100, 1)
    result["completed"] = (
        completed == result["total_modules"] and result["total_modules"] > 0
    )

    capri_dirs = sorted(output_dir.glob("*_caprieval"))
    if capri_dirs:
        last_capri = capri_dirs[-1]
        tsv_files = list(last_capri.glob("capri_clt.tsv"))
        if tsv_files:
            try:
                lines = tsv_files[0].read_text().strip().split("\n")
                if len(lines) >= 2:
                    header = lines[0].split("\t")
                    best = lines[1].split("\t")
                    for i, h in enumerate(header):
                        if i < len(best):
                            result["scores"][h.strip()] = best[i].strip()
            except (OSError, IndexError):
                pass

    log_file = rdir / "log"
    if log_file.exists():
        try:
            lines = log_file.read_text().strip().split("\n")
            result["log_tail"] = lines[-50:]
        except OSError:
            pass

    return result
