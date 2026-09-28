"""Replay-eval harness — shared evaluation substrate (EXECUTION_PLAN §6.5).

One frozen corpus of past sessions; any proposed change runs as an *arm*
against it; a *blind judge* scores the outputs; the report lands in runs/.
Manual invocation only — not a CI gate.

Usage (from opencode_cc_mem/, with the exp tree's src dirs on PYTHONPATH):

    python -m replay_eval run   --arm replay_eval/arms/baseline-master.yaml [--limit N]
    python -m replay_eval judge --run replay_eval/runs/<stamp> [--model <id>]
    python -m replay_eval report --run replay_eval/runs/<stamp>
"""

__version__ = "0.1.0"
