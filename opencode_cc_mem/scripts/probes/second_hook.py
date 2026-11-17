"""Second, independent hook: appends a marker line to $PROBE_LOG2."""
import os
import sys

sys.stdin.read()
with open(os.environ["PROBE_LOG2"], "a", encoding="utf-8") as fh:
    fh.write("second-hook\n")
