"""Tool-semantics manifest — the single source for capture fidelity (A1) and,
later, the receipts extractor's classifications (Motion B).

Verdict condition (2026-09-08 receipts debate): the capture allowlist and the
extractor's classifications must derive from ONE manifest, or they become two
drifting semantics surfaces. Until the CI-generated manifest lands, this
module IS the manifest — add tools and classes here only.
"""

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
