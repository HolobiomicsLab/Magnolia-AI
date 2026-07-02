"""Notebook: generate chronological lab notebook from project memory."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def generate_notebook(
    project_dir: str,
    start_date: str | None = None,
    end_date: str | None = None,
    section: str | None = None,
) -> str:
    """Generate a chronological lab notebook from project memory.

    Merges entries, runs, and session summaries into a single markdown
    timeline grouped by date. Pure read-only view.
    """
    resolved = Path(project_dir).resolve()
    base = resolved / ".magnolia"

    entries_dir = base / "entries"
    runs_dir = base / "runs"
    sessions_dir = base / "sessions"
    session_notes_dir = base / "session-notes"

    # Collect data
    timeline: list[dict] = []

    if section is None or section == "entries":
        timeline.extend(_collect_entries(entries_dir, start_date, end_date))

    if section is None or section == "runs":
        timeline.extend(_collect_runs(runs_dir, start_date, end_date))

    if section is None or section == "sessions":
        timeline.extend(
            _collect_session_summaries(sessions_dir, session_notes_dir, start_date, end_date)
        )

    # Sort by date descending
    timeline.sort(key=lambda x: x.get("date", ""), reverse=True)

    return _render_markdown(resolved.name, timeline, start_date, end_date)


def _collect_entries(entries_dir: Path, start: str | None, end: str | None) -> list[dict]:
    if not entries_dir.exists():
        return []
    results = []
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        text = f.read_text()
        meta = _parse_frontmatter(text)
        date = meta.get("date", "")
        if not _in_range(date, start, end):
            continue
        # Extract body content
        body = _get_body(text)
        results.append({
            "date": date,
            "source": "entry",
            "title": meta.get("title", f.stem),
            "type": meta.get("type", "note"),
            "tags": meta.get("tags", []),
            "filename": f.name,
            "content_preview": body[:150].strip(),
        })
    return results


def _collect_runs(runs_dir: Path, start: str | None, end: str | None) -> list[dict]:
    if not runs_dir.exists():
        return []
    results = []
    for f in runs_dir.glob("*.yaml"):
        if f.name == "INDEX.yaml":
            continue
        try:
            record = yaml.safe_load(f.read_text())
        except yaml.YAMLError:
            continue
        date = record.get("date", "")
        if not _in_range(date, start, end):
            continue
        results.append({
            "date": date,
            "source": "run",
            "tool": record.get("tool", ""),
            "run_id": record.get("run_id", ""),
            "status": record.get("status", ""),
            "metrics": record.get("metrics", {}),
        })
    return results


def _collect_session_summaries(
    sessions_dir: Path,
    session_notes_dir: Path,
    start: str | None,
    end: str | None,
) -> list[dict]:
    """Aggregate session JSONL files into daily summaries."""
    if not sessions_dir.exists():
        return []

    # Group events by date
    daily_events: dict[str, list[dict]] = {}
    for f in sessions_dir.glob("*.jsonl"):
        # Extract date from filename (format: YYYY-MM-DD_HHMMSS.jsonl)
        fname_date = f.stem.split("_")[0]
        if not _in_range(fname_date, start, end):
            continue
        events = []
        with open(f) as fh:
            for line in fh:
                try:
                    events.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
        if fname_date not in daily_events:
            daily_events[fname_date] = []
        daily_events[fname_date].extend(events)

    # Also check session-notes
    notes_by_date: dict[str, str] = {}
    if session_notes_dir.exists():
        for f in session_notes_dir.glob("*.md"):
            fname_date = f.stem.replace("compact_", "").split("_")[0]
            if not _in_range(fname_date, start, end):
                continue
            notes_by_date[fname_date] = f.read_text()[:200]

    results = []
    for date, events in daily_events.items():
        tool_names = list(set(
            ev.get("tool", ev.get("event_type", ""))
            for ev in events
            if ev.get("event_type") in ("tool_call", "tool_success", "tool_error")
        ))
        results.append({
            "date": date,
            "source": "session",
            "event_count": len(events),
            "tools_used": tool_names,
            "summary_note": notes_by_date.get(date, ""),
        })
    return results


def _render_markdown(
    project_name: str,
    timeline: list[dict],
    start: str | None,
    end: str | None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_range = f"{start or 'earliest'} to {end or now}"

    lines = [
        f"# Lab Notebook: {project_name}\n\n",
        f"**Generated:** {now}\n",
        f"**Date range:** {date_range}\n\n",
        "---\n\n",
    ]

    if not timeline:
        lines.append("No records found for this date range.\n")
        return "".join(lines)

    # Group by date
    current_date = None
    for item in timeline:
        date = item.get("date", "unknown")
        if date != current_date:
            current_date = date
            lines.append(f"## {date}\n\n")

        source = item["source"]

        if source == "entry":
            tags = ", ".join(item.get("tags", []))
            lines.append(f"**[{item.get('type', 'note')}]** {item['title']} `tags: {tags}`\n")
            if item.get("content_preview"):
                lines.append(f"> {item['content_preview']}\n")
            lines.append("\n")

        elif source == "run":
            metrics_str = ""
            metrics = item.get("metrics", {})
            if metrics:
                metrics_str = " — " + ", ".join(f"{k}: {v}" for k, v in metrics.items())
            lines.append(
                f"**[{item.get('tool', '')}]** run `{item.get('run_id', '')}`"
                f" — status: {item.get('status', '')}{metrics_str}\n\n"
            )

        elif source == "session":
            tools = ", ".join(item.get("tools_used", []))
            summary = item.get("summary_note", "")
            lines.append(f"**Session:** {item.get('event_count', 0)} events recorded\n")
            if tools:
                lines.append(f"- Tools: {tools}\n")
            if summary:
                lines.append(f"- Summary: {summary[:100]}\n")
            lines.append("\n")

    return "".join(lines)


def _in_range(date: str, start: str | None, end: str | None) -> bool:
    """Check if date falls within [start, end] range."""
    if not date:
        return False
    if start and date < start:
        return False
    if end and date > end:
        return False
    return True


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end].strip()) or {}
    except yaml.YAMLError:
        return {}


def _get_body(text: str) -> str:
    """Extract body content after YAML frontmatter."""
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return text
    return text[end + 3:].strip()
