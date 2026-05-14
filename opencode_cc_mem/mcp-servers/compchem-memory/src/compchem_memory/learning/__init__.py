"""Learning pipeline: post-run assessment, session distillation, consolidation."""

from compchem_memory.learning.assessor import assess_run
from compchem_memory.learning.consolidator import consolidate_tier
from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.compaction import (
    maybe_compact_session,
    compact_session_to_notes,
    estimate_tokens,
    CompactionResult,
)

__all__ = [
    "assess_run",
    "consolidate_tier",
    "AutomaticMemoryExtractor",
    "maybe_compact_session",
    "compact_session_to_notes",
]
