"""DoD gate (EXECUTION_PLAN M1): the extractor reproduces hand-labeled receipts
from 3 archived REAL sessions (hsc70_new x2, bap_e x1 — all pre-A1 v1 records,
which also exercises graceful degradation).

Labels live in tests/fixtures/receipts/expected_*.json, hand-written from the
raw JSONL. The test compares the semantic projection (receipt_id, tool,
fidelity, decisions, outcome) so timestamps/notes don't churn the diff.

Skips when the machine-local corpus (projects/) is absent — fresh clones stay
hermetic; the gate is enforced wherever the archived sessions exist.
"""
import json
from pathlib import Path

import pytest

from compchem_memory.receipts import extract_receipts

FIXTURES = Path(__file__).parent / "fixtures" / "receipts"


def _find_session(rel: str) -> Path | None:
    """The archived corpus is machine-local: it lives in the live tree's
    opencode_cc_mem/projects (the experimental worktree has an empty stub).
    Resolve same-tree first, then the sibling live checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "projects" / rel
        if candidate.exists():
            return candidate
        sibling = (
            parent.parent / "project_magnolia" / "opencode_cc_mem" / "projects" / rel
        )
        if sibling.exists():
            return sibling
    return None


_CASES = [json.loads(f.read_text()) for f in sorted(FIXTURES.glob("expected_*.json"))]
_AVAILABLE = [c for c in _CASES if _find_session(c["session"])]

pytestmark = pytest.mark.skipif(
    not _AVAILABLE,
    reason="machine-local archived session corpus (projects/) absent",
)


def _project(r: dict) -> dict:
    return {
        "receipt_id": r["receipt_id"],
        "tool": r["tool"],
        "fidelity": r["fidelity"],
        "decisions": r["decisions"],
        "outcome": {
            k: v for k, v in (r.get("outcome") or {}).items() if k != "_regex"
        },
    }


@pytest.mark.parametrize("case", _AVAILABLE, ids=[c["session"] for c in _AVAILABLE])
def test_receipts_match_hand_labels_on_archived_sessions(case):
    session_path = _find_session(case["session"])
    assert session_path is not None
    receipts = extract_receipts(session_path)
    got = [_project(r) for r in receipts]
    assert got == case["receipts"]
