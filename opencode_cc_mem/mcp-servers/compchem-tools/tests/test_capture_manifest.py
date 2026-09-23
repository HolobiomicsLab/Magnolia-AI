"""The capture_manifest defaults table must not drift from the live tool
signatures — the receipts extractor's delta-vs-default predicate is only as
honest as this table."""
import inspect

from compchem_memory.capture_manifest import TOOL_DEFAULTS
from compchem_tools.tools.jobs import submit_job


def test_manifest_submit_job_defaults_match_live_signature():
    sig_defaults = {
        k: v.default
        for k, v in inspect.signature(submit_job).parameters.items()
        if v.default is not inspect.Parameter.empty
    }
    for key, expected in TOOL_DEFAULTS["submit_job"].items():
        assert key in sig_defaults, f"{key} not in submit_job signature"
        assert sig_defaults[key] == expected, (
            f"manifest default for {key}={expected!r} drifted from the live "
            f"signature default {sig_defaults[key]!r} — update TOOL_DEFAULTS"
        )
