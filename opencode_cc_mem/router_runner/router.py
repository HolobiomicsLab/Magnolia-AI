"""The frontal Router: deterministic intake, project handoff, follow-up ownership.

Authority (assessment §7.1, §9): the Router classifies, attaches, and proposes —
it NEVER executes scientific work, advances campaign state, promotes memory, or
lets a text message grant authority. All execution paths go through the
control plane's approval + mandate flow.

Robustness contract:
- Project names come from deployment config (registry), never repo code.
- Attachment is EXPLICIT-REFERENCE ONLY (wi:/run:/project ids). No topic-similarity.
- Follow-ups on ACTIVE items become context posts; on TERMINAL items they open a
  new linked Work Item. Human stop blocks any resume.
- Unknown/ambiguous input yields a clarifying question, never a guessed project.
- Untrusted text is data, never instructions.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .agentd import operator
from .engine import ControlEngine
from .models import RouterError, TriggerSpec, WorkItemState, WorkSpec

REF_WI = re.compile(r"\bwi_[a-f0-9]{12}\b")
REF_RUN = re.compile(r"\brun_[a-f0-9]{8,}\b")
REF_TRG = re.compile(r"\btrg_[a-f0-9]{12}\b")
AT_PROJECT = re.compile(r"@([A-Za-z0-9_-]+)")
IN_PROJECT = re.compile(r"^\s*(?:in|on|about|for)\s+([A-Za-z0-9_-]+)\s*[:,;]", re.I)
HOW_IS_PROJECT = re.compile(r"\bhow\s+(?:is|are)\s+([A-Za-z0-9_-]+)\s+doing\b", re.I)
STOP_CMD = re.compile(r"^\s*(?:stop|cancel|kill)\s+(wi_[a-f0-9]{12})\b", re.I)
BACK_TO_ROUTER = re.compile(
    r"^\s*(?:back to router|return to router|switch to|new topic|done here|leave project|exit project)\b",
    re.I,
)
STATUS_WORDS = re.compile(r"\b(progress|status|how is|how's|where are we|update)\b", re.I)
INJECTION = re.compile(
    r"\b(ignore (the )?(gate|rules|policy|previous|above)|bypass|override|jailbreak|"
    r"forget (all )?(your )?(rules|instructions)|do anything you want|sudo)\b",
    re.I,
)


def load_registry() -> dict[str, dict]:
    """Project registry: explicit env > config file > auto-discovery.

    - MAGNOLIA_AGENTD_PROJECTS (comma list) is an explicit allow-list override.
    - ~/.config/magnolia/router-projects.json optionally supplies descriptions.
    - Otherwise discover project dirs (with optional agent.json) under
      MAGNOLIA_PROJECTS_ROOT (default: the repo projects/ and the shared
      project_magnolia-data/projects/). agent.json with enabled:false skips a
      lane. Every discovered project gets <dir>/inbox as its lane inbox.
    - Fallback: {"demo": {}}.
    """
    env = os.environ.get("MAGNOLIA_AGENTD_PROJECTS")
    if env:
        return {p.strip(): {"description": ""} for p in env.split(",") if p.strip()}

    registry = _discover_projects()
    cfg = Path.home() / ".config" / "magnolia" / "router-projects.json"
    if cfg.is_file():
        try:
            for entry in json.loads(cfg.read_text()):
                pid = entry["id"]
                registry.setdefault(pid, {})
                registry[pid].update({k: v for k, v in entry.items() if k != "id"})
        except Exception:
            pass
    return registry or {"demo": {}}


def _discover_projects() -> dict[str, dict]:
    roots: list[Path] = []
    env_root = os.environ.get("MAGNOLIA_PROJECTS_ROOT")
    if env_root:
        roots.append(Path(env_root))
    else:
        roots.append(Path("/home/tjiang/repos/project_magnolia-exp/opencode_cc_mem/projects"))
        roots.append(Path("/home/tjiang/repos/project_magnolia-data/projects"))
    registry: dict[str, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            cfg = d / "agent.json"
            if cfg.is_file():
                try:
                    info = json.loads(cfg.read_text())
                except Exception:
                    info = {}
                if info.get("enabled") is False:
                    continue
                registry[d.name] = {
                    "description": info.get("note", info.get("description", "")),
                    "inbox": str(d / "inbox"),
                }
            else:
                registry.setdefault(d.name, {"description": "", "inbox": str(d / "inbox")})
    return registry


@dataclass
class RouteReply:
    kind: str
    project: str | None = None
    work_item_id: str | None = None
    detail: str = ""
    flags: list[str] = field(default_factory=list)


class Router:
    def __init__(self, engine: ControlEngine, registry: dict[str, dict] | None = None):
        self.engine = engine
        self.registry = registry or load_registry()

    # ---------- public API ----------

    def say(self, text: str) -> RouteReply:
        flags = ["untrusted_text"] if INJECTION.search(text) else []
        if BACK_TO_ROUTER.search(text):
            previous = getattr(self, "_last_project", None)
            self._last_project = None
            return RouteReply(
                "return_to_router",
                project=previous,
                detail=(
                    f"Leaving {previous or 'the project'}; the router session is still open. "
                    "Your next message routes fresh. Work items, memory, and lane sessions persist."
                ),
                flags=flags,
            )
        if STOP_CMD.search(text):
            wi = STOP_CMD.search(text).group(1)
            try:
                self.engine.stop(wi, by=operator())
                return RouteReply("stopped", work_item_id=wi, detail=f"{wi} stopped by operator")
            except RouterError as exc:
                return RouteReply("rejected", work_item_id=wi, detail=str(exc), flags=flags)

        wi_ref = REF_WI.search(text)
        if wi_ref:
            return self._follow_up(wi_ref.group(0), text, flags)

        project_ref = self._explicit_project(text)
        if project_ref == "__unknown__":
            return RouteReply("clarify", detail="unknown project: not in the deployment registry", flags=flags)
        if project_ref:
            if STATUS_WORDS.search(text):
                return RouteReply("progress", project=project_ref,
                                  detail=self._progress(project_ref), flags=flags)
            return self._handoff(project_ref, text, flags)

        # No explicit project: use a single non-demo registry entry, else clarify.
        candidates = [p for p in self.registry if p != "demo"]
        if len(candidates) == 1:
            return self._handoff(candidates[0], text, flags)
        return RouteReply("clarify", detail="which project should this go to?", flags=flags)

    # ---------- internals ----------

    def _explicit_project(self, text: str) -> str | None:
        m = AT_PROJECT.search(text)
        if m:
            return m.group(1) if m.group(1) in self.registry else "__unknown__"
        m = IN_PROJECT.match(text)
        if m:
            return m.group(1) if m.group(1) in self.registry else "__unknown__"
        m = HOW_IS_PROJECT.search(text)
        if m:
            return m.group(1) if m.group(1) in self.registry else "__unknown__"
        return None

    def _handoff(self, project: str, text: str, flags: list[str]) -> RouteReply:
        if project not in self.registry:
            return RouteReply("rejected", detail=f"project not allowed: {project}", flags=flags)
        lane_inbox = self.registry[project].get("inbox")
        if not lane_inbox:
            return RouteReply("clarify", project=project,
                              detail=f"no lane inbox configured for {project}", flags=flags)
        letter = (
            "status: open\n"
            f"project: {project}\n\n"
            "# Routed message\n\n"
            f"{text}\n\n"
            "## Router note\n"
            "Continue the discussion here. If you later submit jobs, report run_ids.\n"
        )
        try:
            Path(lane_inbox).mkdir(parents=True, exist_ok=True)
            from .models import new_id
            path = Path(lane_inbox) / f"{new_id('route')}.task.md"
            path.write_text(letter, encoding="utf-8")
            self._last_project = project
            return RouteReply("handoff", project=project, detail=str(path), flags=flags)
        except OSError as exc:
            return RouteReply("rejected", project=project, detail=f"handoff failed: {exc}", flags=flags)

    def _follow_up(self, work_item_id: str, text: str, flags: list[str]) -> RouteReply:
        try:
            wi = self.engine.store.get("work_items", "work_item_id", work_item_id)
        except Exception:
            wi = None
        if wi is None:
            return RouteReply("clarify", work_item_id=work_item_id,
                              detail="unknown work item", flags=flags)
        if wi["state"] == WorkItemState.STOPPED:
            return RouteReply("rejected", work_item_id=work_item_id,
                              detail="work item is stopped; no automatic resume", flags=flags)
        if wi["state"] in (WorkItemState.COMPLETED, WorkItemState.FAILED):
            result = self.engine.follow_up(work_item_id, operator(), text)
            return RouteReply("work_item", project=wi["project"],
                              work_item_id=result.get("work_item_id"),
                              detail=f"new linked work item after {wi['state']}", flags=flags)
        result = self.engine.follow_up(work_item_id, operator(), text)
        return RouteReply("post", project=wi["project"], work_item_id=work_item_id,
                          detail=f"context post to {result.get('kind')}", flags=flags)

    def _progress(self, project: str) -> str:
        rows = self.engine.store.all("work_items", "project = ?", (project,))
        if not rows:
            return f"{project}: no work items"
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        return f"{project}: {counts}"


def router_from_db(db: str, registry: dict | None = None) -> Router:
    from .agentd import get_engine
    return Router(get_engine(db), registry=registry)

