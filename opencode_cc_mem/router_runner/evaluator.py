"""The LLM Eval stage: a proposer of typed Decisions with confidence.

Two-stage admission (assessment §9.1): the deterministic gate runs first; only
then does an evaluator propose. Available evaluators:
- Jev (a System One decision model) when MAGNOLIA_EVAL_JEV_MODEL is set AND
  present in the opencode-go catalog;
- any generative LLM via llm.call_llm_json as the fallback.
If neither is available, the Router falls back to clarifying, never to guessing.

The evaluator NEVER executes: a Decision is a proposal with a confidence score,
recorded for later measurement. Low-confidence proposals become clarification
(human review), not auto-accepted work.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path

import re

import httpx

# The memory server's llm.py lives in a different package tree; load it by path.
_LLM_PATH = (
    Path(__file__).resolve().parents[1] / "mcp-servers" / "compchem-memory" / "src" / "compchem_memory" / "llm.py"
)
_spec = importlib.util.spec_from_file_location("compchem_llm", _LLM_PATH)
llm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llm)

ACTIONS = {"accept", "attach", "stop", "ignore"}


@dataclass
class EvalDecision:
    action: str
    project: str | None
    confidence: float
    reason: str
    evaluator: str
    work_item_id: str | None = None


def confidence_threshold() -> float:
    try:
        return float(os.environ.get("MAGNOLIA_EVAL_CONFIDENCE_THRESHOLD", "0.7"))
    except ValueError:
        return 0.7


def _catalog_has_model(model: str) -> bool:
    """Whether the opencode-go catalog exposes the configured Jev model id."""
    if not model:
        return False
    v2 = os.environ.get("MAGNOLIA_OPENCODE_V2_BIN", os.path.expanduser(
        "~/.local/share/magnolia-v2/bin/opencode"))
    db = os.path.expanduser("~/.local/share/opencode/opencode-v2.db")
    try:
        out = subprocess.run(
            [v2, "api", "get", "/api/model"],
            env={**os.environ, "OPENCODE_DB": db},
            capture_output=True, text=True, timeout=15, check=False,
        )
        if out.returncode != 0:
            return False
        data = json.loads(out.stdout or "{}")

        def scan(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str) and v == model:
                        return True
                    if scan(v):
                        return True
            elif isinstance(o, list):
                return any(scan(v) for v in o)
            return False

        return scan(data)
    except Exception:
        return False


class GenerativeEvaluator:
    """Fallback: any generative LLM via llm.call_llm_json, JSON Decision out."""

    kind = "generative"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("MAGNOLIA_EVAL_MODEL")

    def available(self) -> bool:
        return llm.is_llm_available()

    def _call_go_model(self, system: str, user: str) -> dict | None:
        """Call an opencode-go model headlessly and parse a JSON Decision."""
        model = self.model or "opencode-go/deepseek-v4.1-flash"
        binary = os.environ.get(
            "MAGNOLIA_OPENCODE_V2_BIN",
            os.path.expanduser("~/.local/share/magnolia-v2/bin/opencode"),
        )
        db = os.path.expanduser("~/.local/share/opencode/opencode-v2.db")
        prompt = f"{system}\n\n{user}"
        try:
            out = subprocess.run(
                [binary, "run", "--model", model, prompt],
                env={**os.environ, "OPENCODE_DB": db},
                capture_output=True, text=True, timeout=90, check=False,
            )
        except Exception:
            return None
        if out.returncode != 0:
            return None
        text = (out.stdout or "").strip()
        if not text:
            return None
        # Accept either a pure JSON object or JSON with a small preamble.
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        return None
        return None

    def evaluate(self, text: str, context: dict | None = None) -> EvalDecision | None:
        if not self.available():
            return None
        if (self.model or "").startswith("opencode-go/"):
            out = self._call_go_model(system="", user=json.dumps({"message": text, "context": context or {}}))
        else:
            system = (
                "You are an admission evaluator. Given a user message and optional "
                "context, return ONLY a JSON object with keys: action "
                "(accept|attach|stop|ignore), project (string or null), work_item_id "
                "(string or null), confidence (0..1), reason (short). "
                "accept means create/route new work to the named project; attach "
                "means follow up an existing Work Item; stop means stop it; ignore "
                "means no action. Never invent project names not present in context."
            )
            user = json.dumps({"message": text, "context": context or {}}, ensure_ascii=False)
            out = llm.call_llm_json(system, user, max_tokens=400, disable_thinking=True)
        if not isinstance(out, dict):
            return None
        action = out.get("action")
        if action not in ACTIONS:
            return None
        try:
            conf = float(out.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return EvalDecision(
            action=action,
            project=out.get("project"),
            confidence=conf,
            reason=str(out.get("reason", "")),
            evaluator=self.kind,
            work_item_id=out.get("work_item_id"),
        )


class JevEvaluator(GenerativeEvaluator):
    """Jev path: the OpenCode Console System One endpoint (free tier).

    Console (not Go plan): POST https://opencode.ai/inference/systemone/v1/systemone
    with model jev-1.13-free (neriousy on issue #53507). A Go-plan balance is
    NOT required. Key resolution: OPENCODE_API_KEY, else auth.json's opencode/
    opencode-go entry, else the v2 DB OAuth access token. If no key is found,
    available() is False and the caller uses the generative evaluator.
    """

    kind = "jev"
    ENDPOINT = "https://opencode.ai/inference/systemone/v1/systemone"

    def __init__(self, model: str | None = None):
        super().__init__(model or os.environ.get("MAGNOLIA_EVAL_JEV_MODEL", "jev-1.13-free"))
        self.jev_model = model

    def _api_key(self) -> str | None:
        if os.environ.get("OPENCODE_API_KEY"):
            return os.environ["OPENCODE_API_KEY"]
        try:
            auth = json.loads((Path.home() / ".local/share/opencode/auth.json").read_text())
            for name in ("opencode", "opencode-go"):
                entry = auth.get(name)
                if isinstance(entry, str) and entry:
                    return entry
                if isinstance(entry, dict):
                    return entry.get("key") or entry.get("access") or entry.get("token")
        except Exception:
            pass
        try:
            import sqlite3
            conn = sqlite3.connect(os.path.expanduser("~/.local/share/opencode/opencode-v2.db"))
            row = conn.execute(
                "select value from credential where integration_id='opencode' and active=1 limit 1"
            ).fetchone()
            if row:
                data = json.loads(row[0])
                return data.get("access")
        except Exception:
            pass
        return None

    def available(self) -> bool:
        return bool(self.jev_model) and bool(self._api_key())

    def evaluate(self, text: str, context: dict | None = None) -> EvalDecision | None:
        if not self.available():
            return None
        projects = (context or {}).get("projects", [])
        project_criteria = {p: p for p in projects} or None
        questions = {
            "action": {
                "type": "choice",
                "instructions": "Which action fits the user request?",
                "criteria": {
                    "accept": "Create or route new work to a project",
                    "attach": "Follow up an existing Work Item",
                    "stop": "Stop an existing Work Item",
                    "ignore": "No action"
                },
            }
        }
        if project_criteria:
            questions["project"] = {
                "type": "choice",
                "instructions": "Which project should handle this work?",
                "criteria": project_criteria,
            }
        body = {
            "model": self.jev_model,
            "state": json.dumps({"message": text, "context": context or {}}, ensure_ascii=False),
            "questions": questions,
        }
        try:
            resp = httpx.post(
                self.ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key()}",
                         "Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None
        answers = data.get("answers") or {}
        if not isinstance(answers, dict):
            return None
        action_answer = answers.get("action") or {}
        action = action_answer.get("choice")
        if action not in ACTIONS:
            return None
        try:
            conf = float(action_answer.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        project_answer = answers.get("project") or {}
        project = project_answer.get("choice") if isinstance(project_answer, dict) else None
        return EvalDecision(
            action=action,
            project=project,
            confidence=conf,
            reason=action_answer.get("reason", action_answer.get("rationale", "")),
            evaluator=self.kind,
            work_item_id=action_answer.get("work_item_id"),
        )


def make_evaluator() -> GenerativeEvaluator | JevEvaluator | None:
    jev_model = os.environ.get("MAGNOLIA_EVAL_JEV_MODEL")
    if jev_model:
        return JevEvaluator(jev_model)
    return GenerativeEvaluator()
