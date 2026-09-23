"""Tool-semantics manifest — the single source for capture fidelity (A1) and,
later, the receipts extractor's classifications (Motion B).

Verdict condition (2026-09-08 receipts debate): the capture allowlist and the
extractor's classifications must derive from ONE manifest, or they become two
drifting semantics surfaces. Until the CI-generated manifest lands, this
module IS the manifest — add tools and classes here only.
"""
from typing import Any


# tool name -> semantic class. Classes drive capture fidelity today and will
# drive receipt-extraction rules tomorrow.
TOOL_CLASSIFICATIONS: dict[str, str] = {
    # Orchestration calls are where parameter decisions are made; a derived
    # receipt can only join args -> result -> run record at full fidelity.
    "submit_job": "orchestration",
}

# Full-fidelity capture (capture_version 2: full args + full result +
# per-record stamp). Derived from the manifest — never a second hand list.
FULL_FIDELITY_TOOLS: frozenset[str] = frozenset(
    name for name, klass in TOOL_CLASSIFICATIONS.items() if klass == "orchestration"
)

# Resource-selection args (the A0/_RESOURCE_KEYS namespace). The receipts
# extractor collapses any deviation among these into ONE
# `resource_selection` decision instead of nine (verdict §3).
RESOURCE_ARG_KEYS: tuple[str, ...] = (
    "ncores", "memory", "time_limit", "scheduler",
    "cluster", "partition", "account", "qos",
)

# Effective defaults per tool, for the delta-vs-default decision predicate.
# A decision = an arg that DIFFERS from this default (args with no default,
# like command/working_dir, are task inputs, not decisions). Drift guard:
# compchem-tools' test suite asserts these match the live submit_job
# signature, so this table cannot silently diverge from the code.
TOOL_DEFAULTS: dict[str, dict[str, Any]] = {
    "submit_job": {
        "scheduler": "slurm",
        "job_name": "compchem",
        "ncores": 4,
        "memory": "4GB",
        "time_limit": "24:00:00",
        "partition": None,
        "cluster": None,
        "account": None,
        "qos": None,
        "tool": None,
        "restart_of": None,
        "remote_precommand": None,
        "acknowledge": False,
        "system_tags": None,
    },
}
