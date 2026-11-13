"""Compatibility shim. Assessment moved to compchem_tools.analysis.

See docs/verified-run-promotion.md for why assessment lives in compchem-tools.
"""

from compchem_tools.analysis.assessor import (  # noqa: F401
    TOOL_ANALYZERS,
    assess_run,
    _expected_outputs,
    _technical_check,
)
