"""Distillation side-effects: the human-readable distill.log and the
.distill-notices queue that the @captured decorator drains into the dialogue.

distill.log  — append-only, every distillation (any path) lands here.
.distill-notices — a transient JSONL queue. Background/inline distillations push;
the @captured decorator drains on the next tool call so the notice reaches the
dialogue.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def _magnolia_dir(project_dir: str) -> Path:
    d = Path(project_dir) / ".magnolia"
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_distill_log(project_dir: str, quote: str, summary: str) -> None:
    """Append a timestamped quote + one-line summary to .magnolia/distill.log.
    Never raises — distillation logging must not break callers."""
    try:
        log = _magnolia_dir(project_dir) / "distill.log"
        ts = datetime.now(timezone.utc).isoformat()
        with open(log, "a") as f:
            f.write(f"[{ts}] {quote}\n")
            f.write(f"[{ts}] {summary}\n")
    except Exception:
        pass


def push_distill_notice(project_dir: str, quote: str, summary: str) -> None:
    """Append a notice to the .distill-notices queue (JSONL). Drained by the
    @captured decorator on the next tool call. Never raises."""
    try:
        queue = _magnolia_dir(project_dir) / ".distill-notices"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "quote": quote,
            "summary": summary,
        }
        with open(queue, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def drain_distill_notices(project_dir: str) -> list[str]:
    """Read and clear the .distill-notices queue. Returns a list of formatted
    notice strings (most-recent-last). Never raises — returns [] on any error."""
    try:
        queue = _magnolia_dir(project_dir) / ".distill-notices"
        if not queue.exists():
            return []
        lines = queue.read_text().splitlines()
        queue.unlink()  # clear the queue
        notices = []
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            notices.append(f"📝 {entry.get('summary', '')} — {entry.get('quote', '')}")
        return notices
    except Exception:
        return []
