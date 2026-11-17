"""Manual recovery for exhausted memory-extraction retries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence


def _is_dialogue_session(store: Path, session_id: str) -> bool:
    from compchem_memory.transcript_source import list_sessions

    dialogue_keys = {
        value
        for ref in list_sessions(store)
        for value in (ref.key, ref.session_id)
    }
    progress = store / "opencode-distilled" / f"{session_id}.json"
    return session_id in dialogue_keys or progress.is_file()


def _retry_dialogue(store: Path, session_id: str) -> dict[str, Any]:
    from compchem_memory.opencode_ingest import distill_session_transcript

    result = distill_session_transcript(
        str(store), session_id, commit=True, force_retry=True
    )
    return result or {
        "status": "retry_failed", "session": session_id, "retry": True,
    }


def _retry_tool_events(
    project: Path, store: Path, session_id: str,
) -> dict[str, Any]:
    from compchem_memory.extraction import AutomaticMemoryExtractor
    from compchem_memory.writer_lease import project_writer_lease

    name = session_id if session_id.endswith(".jsonl") else f"{session_id}.jsonl"
    session_path = store / "sessions" / name
    if not session_path.is_file():
        raise FileNotFoundError(f"memory session not found: {session_id}")
    extractor = AutomaticMemoryExtractor(str(project))
    with project_writer_lease(project):
        extractor._load_state()
        extractor.session_retries.pop(name, None)
        extractor._save_state()
    saved = extractor.commit(session_path, str(project))
    return {
        "status": "committed" if saved is not None else "retry_failed",
        "session": name,
        "saved": saved or [],
        "retry": saved is None,
    }


def retry_session(project_dir: str | Path, session_id: str) -> dict[str, Any]:
    """Clear one session's retry series and attempt extraction exactly once."""
    project = Path(project_dir).resolve()
    if Path(session_id).name != session_id or session_id in {"", ".", ".."}:
        raise ValueError("session must be one filename-safe session id")
    store = project / ".magnolia"
    if _is_dialogue_session(store, session_id):
        return _retry_dialogue(store, session_id)
    return _retry_tool_events(project, store, session_id)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the manual retry command and print its structured result."""
    parser = argparse.ArgumentParser(prog="magnolia memory retry")
    parser.add_argument("session")
    parser.add_argument("--project-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = retry_session(args.project_dir, args.session)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"magnolia memory retry: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("status") == "retry_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
