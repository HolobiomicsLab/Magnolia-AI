"""Context assembly pipeline: multi-stage retrieval with token budget management."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from compchem_memory.retrieval import select_relevant_entries, select_relevant_skills


@dataclass
class BudgetAllocation:
    session_budget: int = 6000
    run_budget: int = 4000
    project_budget: int = 12000
    skill_budget: int = 8000


@dataclass
class ContextAssembly:
    content: str = ""
    tokens_used: int = 0
    sources: list[dict[str, str]] = field(default_factory=list)


def allocate_budget(total: int) -> BudgetAllocation:
    return BudgetAllocation(
        session_budget=min(int(total * 0.20), 6000),
        run_budget=min(int(total * 0.10), 4000),
        project_budget=min(int(total * 0.40), 12000),
        skill_budget=min(int(total * 0.30), 8000),
    )


def _memory_store(project_dir: str) -> Path:
    """Resolve the .magnolia/ memory store directory from a project root."""
    local = Path(project_dir) / ".magnolia"
    if local.is_symlink():
        target = local.resolve()
        if target.exists():
            return target
    return local


def assemble_context(
    task_description: str,
    project_dir: str,
    skills_dir: str,
    token_budget: int = 8000,
    current_run_id: str | None = None,
    conversation_history: list[dict[str, Any]] | None = None,
) -> ContextAssembly:
    allocation = allocate_budget(token_budget)
    store = _memory_store(project_dir)
    sections: list[str] = []
    sources: list[dict[str, str]] = []
    remaining = token_budget

    # 0. Project goal — reference signal, always loaded first
    goal_budget = 500
    goal_ctx = _get_goal(store, goal_budget)
    if goal_ctx:
        sections.append(f"[PROJECT GOAL]\n{goal_ctx}")
        sources.append({"tier": "goal", "id": "GOAL.md"})
        remaining -= _estimate_tokens(goal_ctx)

    # 1. Skills — strategic constraints before tactical data
    #    Protected floor: 30% of total budget is reserved for skills.
    skill_floor = int(token_budget * 0.30)
    skill_entries = select_relevant_skills(
        task_description,
        skills_dir,
        budget=allocation.skill_budget,
    )
    skill_tokens = 0
    for entry in skill_entries:
        content = entry.get("content", "")
        tool = entry.get("tool", "")
        sections.append(f"[SKILL: {tool}]\n{content}")
        sources.append({"tier": "skill", "id": entry.get("filename", "")})
        t = _estimate_tokens(content)
        skill_tokens += t
        remaining -= t

    # Enforce floor: reserve skill_floor tokens for skills.
    # Non-skill content (goal + session + run + project) must not exceed
    # budget minus skill_floor. Cap remaining unconditionally.
    goal_tokens_used = token_budget - remaining - skill_tokens
    remaining = min(remaining, max(0, token_budget - skill_floor - goal_tokens_used))

    # 2. Session context
    session_ctx = _get_session_context(store, allocation.session_budget)
    if session_ctx:
        sections.append(session_ctx)
        sources.append({"tier": "session", "id": "recent"})
        remaining -= _estimate_tokens(session_ctx)

    # 3. Current run state
    if current_run_id:
        run_ctx = _get_run_state(store, current_run_id, allocation.run_budget)
        if run_ctx:
            sections.append(run_ctx)
            sources.append({"tier": "run", "id": current_run_id})
            remaining -= _estimate_tokens(run_ctx)

    # 4. Project entries last — fills whatever budget remains
    if remaining > 2000:
        recent_tools = (
            _extract_recent_tools(conversation_history) if conversation_history else []
        )
        proj_entries = select_relevant_entries(
            task_description,
            str(store),
            budget=min(remaining, allocation.project_budget),
            recent_tools=recent_tools,
        )
        for entry in proj_entries:
            content = entry.get("content", "")
            title = entry.get("title", entry.get("filename", ""))
            sections.append(f"[PROJECT: {title}]\n{content}")
            sources.append({"tier": "project", "id": entry.get("filename", "")})
            remaining -= _estimate_tokens(content)

    combined = "\n\n---\n\n".join(sections)
    tokens = _estimate_tokens(combined)

    return ContextAssembly(
        content=combined[: token_budget * 4] if tokens > token_budget else combined,
        tokens_used=min(tokens, token_budget),
        sources=sources,
    )


def _get_session_context(store: Path, budget: int) -> str | None:
    sessions_dir = store / "sessions"
    if not sessions_dir.exists():
        return None
    session_files = sorted(
        sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not session_files:
        return None
    latest = session_files[0]
    try:
        lines = latest.read_text().strip().split("\n")
    except OSError:
        return None
    recent = lines[-20:]
    events = []
    for line in recent:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        return None
    formatted = json.dumps(events, indent=2)
    return formatted[: budget * 4]


def _get_goal(store: Path, budget: int) -> str | None:
    """Load the project goal from .magnolia/GOAL.md."""
    goal_path = store / "GOAL.md"
    if not goal_path.exists():
        return None
    try:
        content = goal_path.read_text().strip()
        if not content:
            return None
        return content[: budget * 4]
    except OSError:
        return None


def _get_run_state(store: Path, run_id: str, budget: int) -> str | None:
    runs_dir = store / "runs"
    if not runs_dir.exists():
        return None
    import yaml

    for f in runs_dir.glob(f"*{run_id}*.yaml"):
        try:
            data = yaml.safe_load(f.read_text())
            return yaml.dump(data, default_flow_style=False)[: budget * 4]
        except Exception:
            continue
    return None


def _extract_recent_tools(conversation_history: list[dict[str, Any]]) -> list[str]:
    tools = []
    for msg in conversation_history[-10:]:
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            name = tc.get("name", "")
            if "_" in name:
                tool_part = name.split("_")[0]
                tools.append(tool_part)
    return list(set(tools))


def _estimate_tokens(text: str) -> int:
    return len(text) // 4
