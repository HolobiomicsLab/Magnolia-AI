"""The one place the pinned-vs-requested project check lives.

One session = one project. The session is pinned to PROJECT_DIR. A tool called
with a different project_dir is either a legitimate cross-project read (allow,
tag the result) or a cross-project write (block, notify). check_project()
classifies; callers act on the classification.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class GuardResult:
    requested: str          # resolved requested project_dir
    pinned: str             # resolved pinned project_dir
    is_cross_project: bool
    kind: str               # "match" | "cross_read" | "cross_write"


def check_project(
    requested_project_dir: str | None,
    *,
    pinned_dir: str,
    is_write: bool,
) -> GuardResult:
    """Classify a tool's project_dir against the pinned project.

    requested_project_dir=None means "use the pinned project" — always a match.
    Comparison is on resolved paths, so symlinked / non-normalized paths to the
    pinned dir still classify as 'match'.
    """
    pinned_resolved = str(Path(pinned_dir).resolve())
    if requested_project_dir is None:
        return GuardResult(
            requested=pinned_resolved,
            pinned=pinned_resolved,
            is_cross_project=False,
            kind="match",
        )

    requested_resolved = str(Path(requested_project_dir).resolve())
    if requested_resolved == pinned_resolved:
        return GuardResult(
            requested=requested_resolved,
            pinned=pinned_resolved,
            is_cross_project=False,
            kind="match",
        )

    return GuardResult(
        requested=requested_resolved,
        pinned=pinned_resolved,
        is_cross_project=True,
        kind="cross_write" if is_write else "cross_read",
    )
