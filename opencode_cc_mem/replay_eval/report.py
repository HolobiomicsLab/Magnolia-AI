"""Markdown report for a replay-eval run (single arm or an arm bake-off)."""

import json
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = """# Replay-eval report — {title}

- Date: {date}
- Corpus: v{corpus_version} (`{corpus_dir}`) — frozen; capture regime(s): {captures}
- Run dir: `{run_dir}`

## Arms

{arms}

## Extraction results

| arm | slices | ok | parse-fail | finish=length | candidates | total s |
|-----|--------|----|-----------|---------------|------------|---------|
{extraction_rows}

## Blind-judge validity

{judge_section}

## Notes

- Judge model: {judge_model} (blind: IDs only, no arm labels, shuffled).
- Arms must not be compared across capture regimes (`capture_version` above).
- Manual invocation only — this report is evidence for a merge discussion,
  never an automatic gate.
"""


def render_report(run_dir: Path) -> str:
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    judge_path = run_dir / "judge" / "judge_summary.json"
    judge = json.loads(judge_path.read_text(encoding="utf-8")) if judge_path.exists() else None

    arm = summary["arm"]
    arms_md = (f"- **{arm['name']}**: model `{arm['model']}` (provider {arm.get('provider', 'auto')}), "
               f"prompt `{arm['prompt_version']}`, flags `{arm['flags']}`, "
               f"code `{arm['code_ref']}`")

    captures = ", ".join(sorted({r["capture_version"] for r in summary["records"]}))
    extraction_rows = (f"| {arm['name']} | {summary['n_slices']} | {summary['ok']} | "
                       f"{summary['parse_fail']} | {summary['finish_length']} | "
                       f"{summary['total_candidates']} | {summary['total_ms'] / 1000:.1f} |")

    if judge:
        t = judge["totals"]
        ans = max(t.get("answered", t["candidates"]), 1)
        comp = t["candidates"] and t.get("answered", t["candidates"]) / t["candidates"]
        judge_section = (
            f"| metric | value | rate |\n|---|---|---|\n"
            f"| candidates | {t['candidates']} | — |\n"
            f"| answered by judge | {t.get('answered', t['candidates'])} | {comp:.0%} completeness |\n"
            f"| grounded (of answered) | {t['grounded']} | {t['grounded'] / ans:.0%} |\n"
            f"| durable (of answered) | {t['durable']} | {t['durable'] / ans:.0%} |\n"
            f"| specific (of answered) | {t['specific']} | {t['specific'] / ans:.0%} |\n"
            f"| missing verdicts | {t.get('missing', 0)} | scored as missing, not negative |")
    else:
        judge_section = "_Not judged yet — run `python -m replay_eval judge`._"

    return TEMPLATE.format(
        title=arm["name"],
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        corpus_version=summary.get("corpus_version", "?"),
        corpus_dir=summary.get("corpus_dir", "?"),
        captures=captures,
        run_dir=run_dir,
        arms=arms_md,
        extraction_rows=extraction_rows,
        judge_section=judge_section,
        judge_model=judge["judge_model"] if judge else "(pending)",
    )


def write_report(run_dir: Path) -> Path:
    out = Path(run_dir) / "REPORT.md"
    out.write_text(render_report(run_dir), encoding="utf-8")
    return out
