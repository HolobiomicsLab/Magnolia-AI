"""Automatic memory extraction: distill session logs into project-tier entries."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from compchem_memory.storage import ensure_project_store
from compchem_memory.llm import is_llm_available, call_llm_json
from compchem_memory import distill_log
from compchem_memory.reflections import pick_quote

MIN_TOKENS_BETWEEN_EXTRACTIONS = 5000
MIN_TOOL_CALLS_BETWEEN_EXTRACTIONS = 3


# Matches a bare 'exit=N' error message with optional surrounding whitespace
# and no further content. Shell-tool errors that capture only the exit code
# (no stderr text, no stdout, no command) are not informative learnings —
# they used to flood staging with 'Resolved: exit=1'-style entries.
_BARE_EXIT_RE = re.compile(r"^\s*exit\s*=\s*\d+\s*$", re.IGNORECASE)


def _is_meaningful_error(err: str | None) -> bool:
    """True if `err` has diagnostic content worth a memory entry.

    False for: empty/whitespace; a bare 'exit=N' line; the literal
    'Unknown error' fallback.
    """
    if not err or not err.strip():
        return False
    s = err.strip()
    if s.lower() == "unknown error":
        return False
    if _BARE_EXIT_RE.match(s):
        return False
    return True


def has_error_fix_pattern(events: list[dict[str, Any]], window: int = 10) -> bool:
    """True if any tool_error is followed within `window` events by a tool_success
    for the same tool."""
    for i, ev in enumerate(events):
        if ev.get("event_type") != "tool_error":
            continue
        tool = ev.get("tool")
        if not tool:
            continue
        for later in events[i + 1 : i + 1 + window]:
            if later.get("event_type") == "tool_success" and later.get("tool") == tool:
                return True
    return False


def has_significant_result(events: list[dict[str, Any]], project_dir: str) -> bool:
    """True if a run_assessment in the events is significant.
    Three sub-cases:
      1. overall='pass' AND quality_flags=[]
      2. overall='pass' preceded by ≥2 tool_error events on same tool
      3. metrics beat prior best in this project (placeholder — see spec §10)
    """
    for i, ev in enumerate(events):
        if ev.get("event_type") not in ("run_assessment", "post_run_assess"):
            continue
        if ev.get("overall") != "pass":
            continue

        # Case 1: clean run
        if not ev.get("quality_flags"):
            return True

        # Case 2: finally got it working
        tool = ev.get("tool")
        if tool:
            error_count = sum(
                1
                for prior in events[:i]
                if prior.get("event_type") == "tool_error" and prior.get("tool") == tool
            )
            if error_count >= 2:
                return True

        # Case 3: best-in-project — implementation note: compare ev["metrics"] against
        # runs/*.yaml history for tool. Deferred per spec §10.

    return False


EXTRACTION_SYSTEM_PROMPT = """You are extracting durable learnings from a computational chemistry agent
session. Return a JSON array of entries; each has {type, title, content, tags, tools, confidence}.
Skip routine or trivial events — aim for fewer, denser entries.

REQUIREMENTS for `content`:

1. Quantitative grounding. Include specific numbers, identifiers, parameters, and
   measurements from the events: scores, residue numbers, sequence IDs, structure
   counts, protocol names. No vague summaries like "the run worked well" — say
   which run, which score, which sequence.

2. Explicit scope / caveat. If a finding applies to one pocket, one protocol, one
   binding mode, one parameter set: write a `CAVEAT:` section stating exactly what
   conditions the finding is bounded by. Without a caveat, future sessions will
   misapply the entry.

3. Negative findings carry equal weight. If something does NOT work, NOT matter, or
   turned out NOT to be the cause, capture that. "X is not the bottleneck", "Y made
   no difference", "Z is not critical" are first-class learnings.

4. For error_resolution entries, use this structure:
     Symptoms: what was observed (logs, errors, behavior).
     Cause: what was actually wrong.
     Fix: the working solution, with code/commands.
     Also: red herrings eliminated (things that turned out NOT to be the cause).
   The Symptoms section is mandatory — without it, future sessions cannot match
   the entry to their problem.

5. For success_pattern / parameter_guidance entries, include exact parameters that
   produced the result and a mandatory CAVEAT bounding applicability.

USE THE MOST SPECIFIC TYPE:
- `error_resolution`: problem + cause + fix sequence.
- `success_pattern`: noteworthy positive outcome with parameters that produced it.
- `failure_pattern`: an attempted approach that did NOT work, captured to save
  the next attempt.
- `parameter_guidance`: "X worked with parameters Y; Z did not."
- `workflow_note`: ordering / sequencing / process knowledge.
- `note`: ONLY if none of the above fits.

Default to `note` only as a last resort. If the events contain a problem-and-fix
shape, use `error_resolution`. If they contain a result-with-parameters shape, use
`success_pattern` or `parameter_guidance`.

TAGS should include:
- Tool names (haddock3, gromacs, gnina, ...).
- Project-specific identifiers (peptide IDs, protein names, residue numbers,
  pocket IDs, sequence labels).
- Problem domains (peptide-design, hotspot-anchoring, process-management).
Avoid generic single-word tags. Prefer "haddock3 + hotspot-anchoring" to
"haddock3-run".

CONFIDENCE: 0.5–0.7 for first-observation patterns. 0.8–0.95 only if the events
show strong evidence (multiple confirmations within the session, clean metrics,
explicit user confirmation in the log).

prompt_version: v2.0
"""


CONVERSATION_EXTRACTION_PROMPT = """You are extracting durable learnings from the FULL TRANSCRIPT of a
computational chemistry agent conversation — user messages, assistant reasoning, assistant
responses, and tool use. Unlike a tool log, this contains the actual scientific reasoning,
interpretations, decisions, and conclusions. Extract THOSE.

Return a JSON array of entries; each has {type, title, content, tags, tools, confidence}.
Fewer, denser entries. Skip greetings, chit-chat, and routine status.

PRIORITIZE capturing:
- Scientific findings and RELATIONSHIPS stated or concluded in the conversation
  (e.g. "the contact map shows peptide F2 within 4 Angstrom of Hsc70 R272"),
  naming the specific residues / scores / structures / runs involved.
- The user's intent and decisions — what was chosen and WHY.
- Reasoning that explains a result (mechanism, interpretation), flagged as
  hypothesis vs. observation.

Content rules: quantitative grounding (specific numbers/residues/IDs, not vague
summaries); a mandatory CAVEAT bounding applicability; negative findings count
("X is not the cause", "Y made no difference").

USE THE MOST SPECIFIC TYPE:
- `scientific_finding`: a concrete result/relationship about the system under study
  (residues, contacts, scores, binding modes). State HOW it was determined (which
  tool/analysis) so it can be re-verified. Do NOT assert a mechanism from a label
  without the underlying measurement.
- `error_resolution`: problem + cause + fix (Symptoms section mandatory).
- `failure_pattern`: an approach that did NOT work, saved for the next attempt.
- `success_pattern` / `parameter_guidance`: positive outcome / "X worked, Z did not" + exact params.
- `workflow_note`: ordering / process knowledge.
- `note`: last resort only.

TAGS: tool names, project identifiers (peptide IDs, protein names, residue numbers),
problem domains. Avoid generic single-word tags.

CONFIDENCE: 0.5-0.7 for a first observation; 0.8+ only with strong in-conversation
evidence (clean metrics, explicit confirmation).
prompt_version: conv-v1.0
"""


class AutomaticMemoryExtractor:

    def _llm_distill(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Use LLM to extract structured knowledge from session events."""
        events_json = json.dumps(events, indent=2, default=str)
        result = call_llm_json(EXTRACTION_SYSTEM_PROMPT, events_json, max_tokens=4000)
        if not result or not isinstance(result, list):
            return []
        return [r for r in result if isinstance(r, dict) and "title" in r]

    def distill_transcript(self, transcript: str) -> list[dict[str, Any]] | None:
        """Distill a real conversation TRANSCRIPT (not tool events) into candidate
        learnings. This is the path that can capture scientific findings and
        relationships that never appear in the tool log.

        Returns ``None`` when the LLM call itself FAILED (no provider, network
        error, or context overflow — call_llm_json returns None), so the caller
        can retry rather than permanently marking the session done. Returns ``[]``
        only when the LLM succeeded but found nothing worth keeping."""
        if not transcript or not transcript.strip():
            return []
        result = call_llm_json(CONVERSATION_EXTRACTION_PROMPT, transcript, max_tokens=4000)
        if result is None:
            return None  # LLM failed — distinct from "ran and found nothing"
        if not isinstance(result, list):
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

        threshold_met = tokens >= MIN_TOKENS_BETWEEN_EXTRACTIONS and (
            tool_calls >= MIN_TOOL_CALLS_BETWEEN_EXTRACTIONS or not has_pending
        )
        project_dir = str(self.state_path.parent) if self.state_path else ""
        return (
            threshold_met
            or has_error_fix_pattern(since)
            or has_significant_result(since, project_dir)
        )

    def _generate_candidates(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Shared candidate generation: LLM-first, heuristic fallback.
        Pure — reads nothing, writes nothing."""
        candidates: list[dict[str, Any]] = []

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

        return candidates

    def preview(self, session_path: Path) -> list[dict[str, Any]]:
        """Compute candidate learnings and return them. Saves nothing, advances
        no cursor. The read-only inspection mode."""
        events = self._read_events(session_path)
        if not events:
            return []
        return self._generate_candidates(events)

    def commit(self, session_path: Path, project_dir: str) -> list[str]:
        """Compute candidate learnings, save them to staging, advance the cursor.
        Returns the list of saved staging-entry paths."""
        events = self._read_events(session_path)
        if not events:
            return []

        store = ensure_project_store(project_dir)
        candidates = self._generate_candidates(events)

        saved = []
        for candidate in candidates:
            path = self._save_to_staging(store, candidate)
            saved.append(path)

        if saved:
            quote = pick_quote("closing")
            summary = f"{len(saved)} learning(s) distilled from {session_path.name}"
            distill_log.append_distill_log(project_dir, quote, summary)
            distill_log.push_distill_notice(project_dir, quote, summary)

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
                err = ev.get("error", "")
                if not _is_meaningful_error(err):
                    continue  # bare 'exit=N' or empty — no signal, skip
                pending_errors[tool] = err
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
        """Find errors that were never followed by a success for the same tool.

        Skips bare 'exit=N' errors that carry no diagnostic content — those
        produce content-free 'Unresolved failure: exit=1'-style memory entries
        that dilute the signal in staging (cf. 2026-05-30 audit: 115/118
        staging entries were of this form).
        """
        failures = []
        errors: dict[str, dict[str, str]] = {}
        for ev in events:
            etype = ev.get("event_type", "")
            tool = ev.get("tool", "unknown")
            if etype == "tool_error":
                err = ev.get("error", "Unknown error")
                if not _is_meaningful_error(err):
                    continue  # bare 'exit=N' or empty — no signal, skip
                errors[tool] = {
                    "tool": tool,
                    "error": err,
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
