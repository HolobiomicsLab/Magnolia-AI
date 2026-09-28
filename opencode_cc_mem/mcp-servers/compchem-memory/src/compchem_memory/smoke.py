"""Smoke detector — Phase 0 fault-replay + dead-man alarm (plan D3/Phase 0).

Two checks beyond the distiller canary (canary.py):

1. Boot-pipeline duplication: parses boot-timing.jsonl and flags any boot
   segment in which a pipeline step appears more than once. This is the
   signature of the 2026-09-17 double-boot (circular import re-executed the
   server module: two server_import / startup_scan / handover / total rows).
2. Dead-man silence: alarms when the memory LLM layer has produced nothing
   (no llm-timing.jsonl rows) for longer than max_silence_hours WHILE session
   activity continued — the signature of a silently-dead memory layer.

Manual CLI (off the boot path by design):

    python -m compchem_memory.smoke <project_dir> [--boot-timing <path>]

The distill timer calls maybe_run_scheduled() each tick; cadence and kill
switch via MAGNOLIA_SMOKE_INTERVAL_H (hours, default 12, 0 = off).
"""

import json
import os
import time
from pathlib import Path

MARKER_NAME = "last-smoke-run"
REPORT_NAME = "smoke-report.json"
_PIPELINE_STEPS = {"server_import", "startup_scan", "handover", "boot_context",
                   "audit", "total"}


def _marker_path(project_dir: str) -> Path:
    return Path(project_dir) / ".magnolia" / MARKER_NAME


def _default_boot_timing(project_dir: str) -> Path:
    return Path(project_dir) / ".magnolia" / "boot-timing.jsonl"


def check_boot_single_pipeline(boot_timing_path) -> dict:
    """Flag any boot segment with a duplicated pipeline step."""
    path = Path(boot_timing_path)
    if not path.exists():
        return {"status": "pass", "reason": "no boot-timing.jsonl", "boots": 0}
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("step"):
            rows.append(r)

    boots, current = [], []
    for r in rows:
        if r["step"] == "server_import" and current:
            boots.append(current)
            current = []
        current.append(r)
    if current:
        boots.append(current)

    alarms = []
    complete = 0
    for seg in boots:
        steps = [r["step"] for r in seg]
        if "total" not in steps:
            continue  # incomplete (mid-boot at read time) — never alarm on it
        complete += 1
        counts = {}
        for s in steps:
            if s in _PIPELINE_STEPS:
                counts[s] = counts.get(s, 0) + 1
        dupes = sorted(s for s, n in counts.items() if n > 1)
        if dupes:
            alarms.append({"duplicated_steps": dupes, "steps": len(steps)})
    if alarms:
        return {"status": "alarm", "check": "boot_single_pipeline",
                "detail": f"{len(alarms)} boot segment(s) with duplicated pipeline steps",
                "alarms": alarms, "boots": complete}
    return {"status": "pass", "check": "boot_single_pipeline", "boots": complete}


def check_deadman(project_dir: str, max_silence_hours: float = 6.0,
                  now: float | None = None) -> dict:
    """Alarm if session activity continued but the LLM layer went silent.

    'Silent' = newest llm-timing.jsonl row older than max_silence_hours while
    a session JSONL was modified more recently than that row."""
    now = now if now is not None else time.time()
    mag = Path(project_dir) / ".magnolia"
    timing = mag / "llm-timing.jsonl"
    sessions = mag / "sessions"
    if not timing.exists():
        return {"status": "pass", "check": "deadman",
                "reason": "no llm-timing.jsonl yet"}
    last_llm = timing.stat().st_mtime
    newest_session = 0.0
    if sessions.exists():
        for f in sessions.glob("*.jsonl"):
            newest_session = max(newest_session, f.stat().st_mtime)
    silence_h = (now - last_llm) / 3600.0
    if silence_h > max_silence_hours and newest_session > last_llm:
        return {"status": "alarm", "check": "deadman",
                "detail": (f"no LLM activity for {silence_h:.1f}h while session "
                           f"logs were active — memory layer may be silently dead")}
    return {"status": "pass", "check": "deadman", "silence_hours": round(silence_h, 2)}


def run_all(project_dir: str, boot_timing_path=None,
            max_silence_hours: float = 6.0, run_canary_check: bool = True) -> dict:
    """Full smoke pass: distiller canary + boot duplication + dead-man."""
    from compchem_memory import canary

    checks = []
    if run_canary_check:
        checks.append(canary.run_canary(project_dir))
    checks.append(check_boot_single_pipeline(
        boot_timing_path or _default_boot_timing(project_dir)))
    checks.append(check_deadman(project_dir, max_silence_hours))

    alarmed = [c for c in checks if c.get("status") == "alarm"]
    failed = [c for c in checks if c.get("status") == "fail"]
    report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "status": "alarm" if alarmed else ("fail" if failed else "pass"),
              "checks": checks}
    out = Path(project_dir) / ".magnolia" / REPORT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def maybe_run_scheduled(project_dir: str) -> dict | None:
    """Called from the distill-timer tick. Runs the smoke pass when the
    marker is older than MAGNOLIA_SMOKE_INTERVAL_H (default 12; 0 disables).
    Returns the report, or None when skipped. Never raises."""
    try:
        interval_h = float(os.environ.get("MAGNOLIA_SMOKE_INTERVAL_H", "12"))
    except ValueError:
        interval_h = 12.0
    if interval_h <= 0:
        return None
    marker = _marker_path(project_dir)
    now = time.time()
    if marker.exists() and (now - marker.stat().st_mtime) < interval_h * 3600:
        return None
    try:
        report = run_all(project_dir)
    except Exception as exc:  # never crash the timer
        return {"status": "error", "detail": str(exc)[:300]}
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(report["ts"] + "\n", encoding="utf-8")
    if report["status"] != "pass":
        print(f"[smoke] {report['status']}: "
              + "; ".join(c.get("detail", c.get("reason", ""))
                          for c in report["checks"] if c["status"] != "pass"))
    return report


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="compchem_memory.smoke")
    ap.add_argument("project_dir")
    ap.add_argument("--boot-timing", default=None)
    ap.add_argument("--max-silence-hours", type=float, default=6.0)
    ap.add_argument("--skip-canary", action="store_true",
                    help="skip the LLM probe (boot + deadman checks only)")
    args = ap.parse_args(argv)
    report = run_all(args.project_dir, args.boot_timing,
                     args.max_silence_hours, run_canary_check=not args.skip_canary)
    print(json.dumps(report, indent=1))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
