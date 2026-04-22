"""Automatic memory extraction: distill session logs into project-tier entries."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from compchem_memory.storage import ensure_project_store
from compchem_memory.llm import is_llm_available, call_llm_json

MIN_TOKENS_BETWEEN_EXTRACTIONS = 5000
MIN_TOOL_CALLS_BETWEEN_EXTRACTIONS = 3

EXTRACTION_SYSTEM_PROMPT = (
    "You are a computational chemistry memory extraction agent. "
    "Analyze the following session events and extract structured knowledge. "
    "Return a JSON array of extracted entries. Each entry should have:\n"
    '- "type": one of "error_resolution", "success_pattern", "failure_pattern", "parameter_guidance", "workflow_note"\n'
    '- "title": concise descriptive title\n'
    '- "content": detailed markdown content\n'
    '- "tags": list of relevant tags\n'
    '- "tools": list of tools mentioned\n'
    '- "confidence": float 0.0-1.0\n'
    "Only extract genuinely useful knowledge. Skip trivial events."
)


class AutomaticMemoryExtractor:

    def _llm_distill(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Use LLM to extract structured knowledge from session events."""
        events_json = json.dumps(events, indent=2, default=str)
        result = call_llm_json(EXTRACTION_SYSTEM_PROMPT, events_json, max_tokens=4000)
        if not result or not isinstance(result, list):
            return []
        return [r for r in result if isinstance(r, dict) and "title" in r]
    def __init__(self, project_dir: str | None = None):
        self.last_cursor: str = ""
        self.state_path: Path | None = None
        if project_dir:
            self.state_path = Path(project_dir) / "extraction-state.yaml"
            self._load_state()

    def should_extract(self, session_path: Path) -> bool:
        if not session_path.exists():
            return False
        events = self._read_events(session_path)
        if not events:
            return False

        cursor_idx = 0
        if self.last_cursor:
            for i, ev in enumerate(events):
                if ev.get("timestamp", "") == self.last_cursor:
                    cursor_idx = i + 1
                    break

        since = events[cursor_idx:]
        if not since:
            return False

        text = json.dumps(since)
        tokens = len(text) // 4
        tool_calls = sum(
            1
            for ev in since
            if ev.get("event_type") in ("tool_call", "tool_success", "tool_error")
        )

        has_pending = any(
            ev.get("event_type") == "tool_call"
            and not any(
                e.get("event_type") in ("tool_success", "tool_error")
                and e.get("tool") == ev.get("tool")
                for e in since[since.index(ev) + 1 :]
            )
            for ev in since
        )

        return tokens >= MIN_TOKENS_BETWEEN_EXTRACTIONS and (
            tool_calls >= MIN_TOOL_CALLS_BETWEEN_EXTRACTIONS or not has_pending
        )

    def extract_and_save(self, session_path: Path, project_dir: str) -> list[str]:
        events = self._read_events(session_path)
        if not events:
            return []

        store = ensure_project_store(project_dir)
        candidates = []

        # Try LLM-based extraction first
        if is_llm_available():
            llm_candidates = self._llm_distill(events)
            if llm_candidates:
                candidates = llm_candidates

        # Fallback to heuristic extraction
        if not candidates:
            for err, resolution in self._find_error_resolutions(events):
                candidates.append(
                    {
                        "type": "error_resolution",
                        "title": f"Resolved: {err[:80]}",
                        "content": f"## Error\n{err}\n\n## Resolution\n{resolution}",
                        "tags": ["error-resolution", "auto"],
                        "tools": [],
                        "confidence": 0.8,
                    }
                )

            for pattern in self._find_success_patterns(events):
                candidates.append(
                    {
                        "type": "success_pattern",
                        **pattern,
                        "tags": pattern.get("tags", ["success-pattern", "auto"]),
                        "confidence": 0.8,
                    }
                )

            for param_info in self._find_parameter_guidance(events):
                candidates.append(
                    {
                        "type": "parameter_guidance",
                        "title": f"Non-default parameter: {param_info['param']}",
                        "content": (
                            f"Tool: {param_info['tool']}\n"
                            f"Parameter: {param_info['param']}\n"
                            f"Value: {param_info['value']}\n"
                            f"Default: {param_info['default']}"
                        ),
                        "tags": ["parameter", param_info["tool"]],
                        "tools": [param_info["tool"]],
                        "confidence": 0.6,
                    }
                )

            for fail_info in self._find_unresolved_failures(events):
                candidates.append(
                    {
                        "type": "failure_pattern",
                        "title": f"Unresolved failure: {fail_info['error'][:80]}",
                        "content": (
                            f"## Tool\n{fail_info['tool']}\n\n"
                            f"## Error\n{fail_info['error']}\n\n"
                            f"## Context\n{fail_info.get('context', 'No additional context')}"
                        ),
                        "tags": ["failure-pattern", "unresolved", fail_info["tool"]],
                        "tools": [fail_info["tool"]],
                        "confidence": 0.4,
                    }
                )

        saved = []
        for candidate in candidates:
            path = self._save_to_staging(store, candidate)
            saved.append(path)

        if events:
            self.last_cursor = events[-1].get("timestamp", "")
            self._save_state()

        return saved

    def _load_state(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                data = yaml.safe_load(self.state_path.read_text()) or {}
                self.last_cursor = data.get("last_cursor", "")
            except (yaml.YAMLError, OSError):
                pass

    def _save_state(self) -> None:
        if not self.state_path:
            return
        state = {
            "last_cursor": self.last_cursor,
            "last_extraction": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.state_path.write_text(yaml.dump(state, default_flow_style=False))
        except OSError:
            pass

    def _find_error_resolutions(
        self, events: list[dict[str, Any]]
    ) -> list[tuple[str, str]]:
        pairs = []
        pending_errors: dict[str, str] = {}
        for ev in events:
            etype = ev.get("event_type", "")
            tool = ev.get("tool", "unknown")
            if etype == "tool_error":
                pending_errors[tool] = ev.get("error", "")
            elif etype == "tool_success" and tool in pending_errors:
                pairs.append(
                    (
                        pending_errors[tool],
                        ev.get("result_summary", "Retried successfully"),
                    )
                )
                del pending_errors[tool]
        return pairs

    def _find_success_patterns(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        patterns = []
        post_assess = [ev for ev in events if ev.get("event_type") == "post_run_assess"]
        for ev in post_assess:
            assessment = ev.get("assessment", {})
            if assessment.get("overall") == "pass":
                metrics = assessment.get("metrics", {})
                tool = assessment.get("tool", "unknown")
                score = metrics.get("score", metrics.get("best_score", ""))
                patterns.append(
                    {
                        "title": f"Successful {tool} run (score: {score})",
                        "content": f"Tool: {tool}\nMetrics: {json.dumps(metrics)}",
                        "tools": [tool],
                    }
                )
        return patterns

    def _find_parameter_guidance(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        params = []
        for ev in events:
            if ev.get("event_type") == "tool_call" and ev.get("non_default_params"):
                for p in ev["non_default_params"]:
                    params.append(p)
        return params

    def _find_unresolved_failures(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Find errors that were never followed by a success for the same tool."""
        failures = []
        errors: dict[str, dict[str, str]] = {}
        for ev in events:
            etype = ev.get("event_type", "")
            tool = ev.get("tool", "unknown")
            if etype == "tool_error":
                errors[tool] = {
                    "tool": tool,
                    "error": ev.get("error", "Unknown error"),
                    "context": ev.get("result_summary", ""),
                }
            elif etype == "tool_success" and tool in errors:
                del errors[tool]
        for info in errors.values():
            failures.append(info)
        return failures

    def _save_to_staging(self, store: Path, candidate: dict[str, Any]) -> str:
        staging_dir = store / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        title = candidate.get("title", "untitled")
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:60].strip("_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        fname = f"{ts}_{slug}.md"
        fpath = staging_dir / fname

        frontmatter = {
            "id": ts,
            "type": candidate.get("type", "note"),
            "title": title,
            "description": candidate.get("content", "")[:200],
            "tools": candidate.get("tools", []),
            "tags": candidate.get("tags", []),
            "created": datetime.now(timezone.utc).isoformat(),
            "updated": datetime.now(timezone.utc).isoformat(),
            "source": "auto_extraction",
            "observation_count": 1,
            "confidence": candidate.get("confidence", 0.5),
        }
        fm_str = "---\n" + yaml.dump(frontmatter, default_flow_style=False) + "---\n\n"
        fpath.write_text(fm_str + candidate.get("content", "") + "\n")
        return str(fpath)

    def _read_events(self, session_path: Path) -> list[dict[str, Any]]:
        events = []
        try:
            with open(session_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass
        return events
