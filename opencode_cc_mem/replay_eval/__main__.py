"""CLI: python -m replay_eval {run,judge,report}"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="replay_eval")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run one arm over the corpus")
    p_run.add_argument("--arm", required=True)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--corpus", default=str(PKG_DIR / "corpus"))
    p_run.add_argument("--out", default=None)

    p_judge = sub.add_parser("judge", help="blind-judge a run dir")
    p_judge.add_argument("--run", required=True)
    p_judge.add_argument("--model", default="kimi-k3-0711-preview")
    p_judge.add_argument("--provider", default=None)
    p_judge.add_argument("--temperature", type=float, default=0.0)
    p_judge.add_argument("--max-tokens", type=int, default=8000)
    p_judge.add_argument("--corpus", default=str(PKG_DIR / "corpus"))

    p_rep = sub.add_parser("report", help="render REPORT.md for a run dir")
    p_rep.add_argument("--run", required=True)

    p_res = sub.add_parser("rescore", help="recompute judge totals from existing verdict files (no LLM)")
    p_res.add_argument("--run", required=True)
    p_res.add_argument("--judge-dir", default="judge")

    args = ap.parse_args(argv)

    from replay_eval import loader

    if args.cmd == "run":
        from replay_eval import runner

        corpus = loader.load_corpus(Path(args.corpus))
        arm = runner.load_arm(Path(args.arm))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        out = Path(args.out) if args.out else PKG_DIR / "runs" / f"{stamp}_{arm['name']}"
        summary = runner.run_arm(corpus, arm, out, limit=args.limit)
        print(f"run dir: {out}")
        print(f"slices={summary['n_slices']} ok={summary['ok']} "
              f"parse_fail={summary['parse_fail']} candidates={summary['total_candidates']}")
        return 0

    if args.cmd == "judge":
        from replay_eval import judge

        corpus = loader.load_corpus(Path(args.corpus))
        res = judge.judge_run(args.run, corpus, args.model, args.provider,
                              temperature=args.temperature, max_tokens=args.max_tokens)
        t = res["totals"]
        print(f"judged {t['candidates']} candidates: grounded={t['grounded']} "
              f"durable={t['durable']} specific={t['specific']}")
        return 0

    if args.cmd == "report":
        from replay_eval import report

        print(report.write_report(args.run))
        return 0

    if args.cmd == "rescore":
        from replay_eval import judge

        t = judge.rescore(args.run, args.judge_dir)["totals"]
        comp = t["answered"] / max(t["candidates"], 1)
        print(f"answered {t['answered']}/{t['candidates']} ({comp:.0%}); "
              f"grounded {t['grounded']} ({t['grounded'] / max(t['answered'], 1):.0%} of answered); "
              f"durable {t['durable']}; missing {t['missing']}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
