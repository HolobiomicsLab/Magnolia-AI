"""Distiller canary + freeze-on-canary (plan D3, 2026-09-18).

Runs a fixed probe transcript through the real distiller on a schedule (cron,
agent, or manual CLI). If extraction drifts — empty result, LLM failure, lost
topics — a flag file `<project>/.magnolia/canary-frozen` is written and the
auto-promotion path halts until the canary clears. This exists because the
distiller has failed silently before (placeholder model config, thinking-mode
blank outputs, vendor model-alias swaps).
"""

import argparse
import json
from pathlib import Path

CANARY_TRANSCRIPT = """user: run the haddock3 docking for peptide KFERQ against receptor 4PO2.

assistant (reasoning): I will prepare the actpass restraint file and launch a
rigidbody docking at sampling 1000 on the cluster.

assistant: Submitting the haddock3 run with restraints from actpass; output goes
to runs/2026-01-01_canary_docking/.

user: the run completed with caprieval score -55.3 and 4 clusters.

assistant: Docking succeeded; the summary is written to the run directory.
"""

CANARY_EXPECT = "haddock3"
FLAG_NAME = "canary-frozen"


def flag_path(project_dir: str) -> Path:
    return Path(project_dir) / ".magnolia" / FLAG_NAME


def is_frozen(project_dir: str) -> bool:
    return flag_path(project_dir).exists()


def clear(project_dir: str) -> None:
    p = flag_path(project_dir)
    if p.exists():
        p.unlink()


def run_canary(
    project_dir: str,
    distiller=None,
    transcript: str | None = None,
    expect: str = CANARY_EXPECT,
) -> dict:
    """Distill the probe transcript; on any drift, write the freeze flag.

    Returns {"status": "pass", "candidates": N} or
    {"status": "fail", "reason": ...}.
    """
    if distiller is None:
        from compchem_memory.extraction import AutomaticMemoryExtractor

        distiller = AutomaticMemoryExtractor().distill_transcript
    text = transcript if transcript is not None else CANARY_TRANSCRIPT
    detail: dict = {}
    result = None
    try:
        result = distiller(text)
    except Exception as exc:  # canary must never crash the caller
        detail["exception"] = str(exc)[:300]
    if result is None:
        detail["reason"] = "distiller returned None (LLM failure)"
    elif not isinstance(result, list) or len(result) == 0:
        detail["reason"] = f"expected >=1 candidate, got {result!r}"[:300]
    elif not any(expect in json.dumps(r).lower() for r in result):
        detail["reason"] = f"expected topic '{expect}' missing from candidates"

    if result is None or detail.get("reason"):
        flag = flag_path(project_dir)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps({"status": "fail", **detail}) + "\n")
        return {"status": "fail", **detail}

    clear(project_dir)
    return {"status": "pass", "candidates": len(result)}


def main() -> int:
    ap = argparse.ArgumentParser(prog="canary")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--clear", action="store_true", help="remove the freeze flag")
    args = ap.parse_args()
    if args.clear:
        clear(args.project_dir)
        print("canary flag cleared")
        return 0
    report = run_canary(args.project_dir)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
