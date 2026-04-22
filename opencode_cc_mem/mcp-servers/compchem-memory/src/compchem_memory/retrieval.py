"""Semantic memory retrieval: heuristic scoring + header-based selection."""

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from compchem_memory.scanning import scan_memory_headers, scan_skills_headers
from compchem_memory.llm import is_llm_available, call_llm_json


def llm_select_memories(
    task_description: str,
    headers: list[dict[str, Any]],
    max_selections: int = 5,
) -> list[str] | None:
    """Use LLM to select the most relevant memory entries. Returns list of filenames or None."""
    from compchem_memory.scanning import format_manifest

    system_prompt = (
        "Select the most relevant memory entries for this task. "
        'Return JSON: {"selected": ["filename1.md", "filename2.md", ...]}'
    )
    manifest = format_manifest(headers)
    user_content = f"Task: {task_description}\n\nAvailable entries:\n{manifest}"
    result = call_llm_json(system_prompt, user_content)
    if result and isinstance(result, dict) and "selected" in result:
        selected = result["selected"][:max_selections]
        return selected
    return None


def select_relevant_entries(
    task_description: str,
    project_dir: str,
    budget: int = 12000,
    max_selections: int = 5,
    max_per_entry: int = 5000,
    recent_tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    entries_dir = Path(project_dir) / "entries"
    headers = scan_memory_headers(entries_dir)
    if recent_tools:
        headers = [
            h for h in headers if not any(t in h.get("tools", []) for t in recent_tools)
        ]
    task_lower = task_description.lower()
    task_words = set(task_lower.split())
    scored: list[tuple[float, dict[str, Any]]] = []
    for h in headers:
        score = _score_entry(h, task_lower, task_words)
        if score > 0:
            scored.append((score, h))
    scored.sort(key=lambda x: x[0], reverse=True)

    # Phase 2: LLM picks from top-15 candidates if available
    top_candidates = [h for _, h in scored[:15]]
    selected_filenames = None
    if is_llm_available() and top_candidates:
        selected_filenames = llm_select_memories(task_description, top_candidates, max_selections)

    if selected_filenames:
        # Use LLM-selected filenames, in the order returned
        filename_set = set(selected_filenames)
        final_headers = [h for h in top_candidates if h["filename"] in filename_set]
        # Preserve LLM ordering
        fname_to_h = {h["filename"]: h for h in top_candidates}
        final_headers = [fname_to_h[fn] for fn in selected_filenames if fn in fname_to_h]
    else:
        final_headers = top_candidates[:max_selections]

    results: list[dict[str, Any]] = []
    used_tokens = 0
    for h in final_headers:
        if len(results) >= max_selections:
            break
        path = Path(h["path"])
        content = _load_entry_content(path)
        if content is None:
            continue
        tokens = _estimate_tokens(content)
        if tokens > max_per_entry:
            content = content[: max_per_entry * 4]
            tokens = max_per_entry
        if used_tokens + tokens > budget:
            continue
        results.append({**h, "content": content, "relevance_score": round(_score_entry(h, task_lower, task_words), 2)})
        used_tokens += tokens
    return results


def select_relevant_skills(
    task_description: str,
    skills_dir: str,
    budget: int = 8000,
) -> list[dict[str, Any]]:
    headers = scan_skills_headers(Path(skills_dir))
    task_lower = task_description.lower()
    task_words = set(task_lower.split())
    scored: list[tuple[float, dict[str, Any]]] = []
    for h in headers:
        score = 0.0
        tool_name = h.get("tool", "")
        desc = h.get("description", "").lower()
        for word in task_words:
            if word in tool_name.lower():
                score += 5.0
            if word in desc:
                score += 2.0
        if score > 0:
            scored.append((score, h))
    scored.sort(key=lambda x: x[0], reverse=True)

    # Phase 2: LLM picks if available
    top_candidates = [h for _, h in scored[:15]]
    selected_filenames = None
    if is_llm_available() and top_candidates:
        selected_filenames = llm_select_memories(task_description, top_candidates, max_selections=5)

    if selected_filenames:
        fname_to_h = {h["filename"]: h for h in top_candidates}
        final_headers = [fname_to_h[fn] for fn in selected_filenames if fn in fname_to_h]
    else:
        final_headers = top_candidates

    results: list[dict[str, Any]] = []
    used_tokens = 0
    for h in final_headers:
        if used_tokens >= budget:
            break
        path = Path(h["path"])
        content = _load_entry_content(path)
        if content is None:
            continue
        tokens = _estimate_tokens(content)
        if used_tokens + tokens > budget:
            content = content[: (budget - used_tokens) * 4]
            tokens = _estimate_tokens(content)
        results.append({**h, "content": content, "relevance_score": round(
            sum(5.0 if w in h.get("tool", "").lower() else 0 for w in task_words) +
            sum(2.0 if w in h.get("description", "").lower() else 0 for w in task_words), 2
        )})
        used_tokens += tokens
    return results


def _score_entry(
    header: dict[str, Any], task_lower: str, task_words: set[str]
) -> float:
    title = header.get("title", "").lower()
    desc = header.get("description", "").lower()
    tags = [t.lower() for t in header.get("tags", [])]
    tools = [t.lower() for t in header.get("tools", [])]
    entry_type = header.get("type", "note")
    type_boost = {
        "error_resolution": 2.5,
        "success_pattern": 2.5,
        "failure_pattern": 2.5,
        "parameter_guidance": 2.0,
        "workflow_note": 1.5,
        "note": 1.0,
    }
    score = type_boost.get(entry_type, 1.0)
    confidence = header.get("confidence", 0.5)
    score *= 0.5 + confidence * 0.5
    observations = header.get("observation_count", 0)
    if observations >= 3:
        score *= 1.2

    # Confidence decay: entries not recently verified lose relevance.
    # Half-life ~60 days; lambda = ln(2)/60 ≈ 0.01155
    verified_str = header.get("last_verified", header.get("date", ""))
    if verified_str:
        try:
            verified_date = datetime.fromisoformat(verified_str).date()
            age_days = (datetime.now(timezone.utc).date() - verified_date).days
            score *= math.exp(-0.01155 * max(age_days, 0))
        except (ValueError, TypeError):
            pass  # unparsable date — skip decay

    for word in task_words:
        if word in title:
            score += 5.0
        if word in desc:
            score += 2.0
        for tag in tags:
            if word in tag:
                score += 3.0
        for tool in tools:
            if word in tool:
                score += 4.0
    return score


def _load_entry_content(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text()
    except OSError:
        return None


def _estimate_tokens(text: str) -> int:
    return len(text) // 4
