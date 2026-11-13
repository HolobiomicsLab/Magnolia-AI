"""Compatibility shim. Assessment orchestration moved to compchem_tools.analysis.

See docs/verified-run-promotion.md for why assessment lives in compchem-tools.
"""

from compchem_tools.analysis.orchestrator import *  # noqa: F401,F403
