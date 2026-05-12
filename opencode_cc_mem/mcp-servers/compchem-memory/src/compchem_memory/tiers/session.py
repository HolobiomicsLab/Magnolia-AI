"""Session tier: append-only JSONL log for the current conversation."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


class SessionManager:
    def __init__(
        self,
        sessions_dir: Path,
        project_id: str | None = None,
        project_dir: str | None = None,
    ):
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.project_id = project_id or "unknown"
        self.project_dir = project_dir or ""
        self._current_session: Path | None = None
        self._current_session_id: str | None = None
        self._discover_todays_session()

    def _discover_todays_session(self) -> None:
        """If a session file for today exists for this project, adopt it.
        Otherwise stay unset; first record() will create a new one.
        Survives MCP server restarts within a single user-session."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        candidates = sorted(self.sessions_dir.glob(f"{today}_*.jsonl"), reverse=True)
        for cand in candidates:
            try:
                first = cand.read_text().splitlines()[0]
                header = json.loads(first)
            except (IndexError, json.JSONDecodeError):
                continue
            if header.get("event_type") == "session_start" and header.get("project_id") == self.project_id:
                self._current_session = cand
                self._current_session_id = header.get("session_id", cand.stem)
                return

    def _get_session_path(self) -> Path:
        if self._current_session and self._current_session.exists():
            return self._current_session
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        self._current_session_id = ts
        fname = f"{ts}.jsonl"
        self._current_session = self.sessions_dir / fname
        self._write_header()
        return self._current_session

    def _write_header(self) -> None:
        header = {
            "event_type": "session_start",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self._current_session_id,
            "project_id": self.project_id,
            "project_dir": self.project_dir,
            "schema_version": SCHEMA_VERSION,
        }
        with open(self._current_session, "a") as f:
            f.write(json.dumps(header) + "\n")

    def record(
        self,
        event_type: str,
        data: dict[str, Any],
        project_dir: str | None = None,
    ) -> str:
        path = self._get_session_path()
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "session_id": self._current_session_id,
            "project_id": self.project_id,
            **data,
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return str(path)

    def get_recent(
        self, n: int = 50, project_dir: str | None = None
    ) -> list[dict[str, Any]]:
        path = self._get_session_path()
        if not path.exists():
            return []
        with open(path) as f:
            lines = f.readlines()
        events = []
        for line in lines[-n:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def search(
        self, keyword: str, project_dir: str | None = None
    ) -> list[dict[str, Any]]:
        path = self._get_session_path()
        if not path.exists():
            return []
        results = []
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if keyword.lower() in json.dumps(entry).lower():
                    results.append(entry)
        return results

    def start_new_session(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        self._current_session_id = ts
        self._current_session = self.sessions_dir / f"{ts}.jsonl"
        self._write_header()
        return str(self._current_session)

    def get_session_log_path(self) -> str | None:
        if self._current_session and self._current_session.exists():
            return str(self._current_session)
        return None

    def count_events_since(self, cursor: str) -> tuple[int, int]:
        path = self._get_session_path()
        if not path.exists():
            return 0, 0
        events = []
        cursor_idx = 0
        with open(path) as f:
            for line in f:
                try:
                    ev = json.loads(line.strip())
                    events.append(ev)
                except json.JSONDecodeError:
                    continue
        if cursor:
            for i, ev in enumerate(events):
                if ev.get("timestamp", "") == cursor:
                    cursor_idx = i + 1
                    break
        since = events[cursor_idx:]
        text = json.dumps(since)
        tokens = len(text) // 4
        tool_calls = sum(
            1
            for ev in since
            if ev.get("event_type") in ("tool_call", "tool_success", "tool_error")
        )
        return tokens, tool_calls
