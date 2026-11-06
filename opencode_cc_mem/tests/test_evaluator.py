"""Tests for the LLM Eval stage (Jev Console + generative fallback).

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_evaluator.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.evaluator import EvalDecision, JevEvaluator, make_evaluator
from router_runner.router import Router


def make_router(tmp_path, evaluator=None):
    engine = agentd.get_engine(str(tmp_path / "agentd.db"), out_root=tmp_path / "runs")
    registry = {
        "proj_a": {"inbox": str(tmp_path / "proj_a" / "inbox")},
        "proj_b": {"inbox": str(tmp_path / "proj_b" / "inbox")},
        "demo": {},
    }
    if evaluator is None:
        class _NoEval:
            def available(self):
                return False
            def evaluate(self, text, context=None):
                return None
        evaluator = _NoEval()
    return Router(engine, registry=registry, evaluator=evaluator)


def test_jev_accept_with_confidence_routes_and_records(tmp_path):
    decision = EvalDecision("accept", "proj_a", 0.95, "clear docking request", "jev")

    class FakeJev:
        def available(self):
            return True

        def evaluate(self, text, context=None):
            return decision

    router = make_router(tmp_path, evaluator=FakeJev())
    reply = router.say("dock the peptide")
    assert reply.kind == "handoff"
    assert reply.project == "proj_a"
    assert "eval" in reply.flags
    rows = router.engine.store.all("eval_decisions")
    assert len(rows) == 1
    assert rows[0]["evaluator"] == "jev"
    assert rows[0]["confidence"] == 0.95
    # No execution happened: only the lane letter was written.
    assert router.engine.store.all("effects", "kind = ?", ("campaign_run",)) == []


def test_low_confidence_clarifies(tmp_path):
    class Low:
        def available(self):
            return True

        def evaluate(self, text, context=None):
            return EvalDecision("accept", "proj_a", 0.2, "unclear", "generative")

    reply = make_router(tmp_path, evaluator=Low()).say("analyze this")
    assert reply.kind == "clarify"
    assert "low confidence" in reply.detail


def test_eval_stop_and_attach(tmp_path):
    wi = _ingest(tmp_path)

    class Stop:
        def available(self):
            return True

        def evaluate(self, text, context=None):
            return EvalDecision("stop", None, 0.9, "stop it", "jev", work_item_id=wi)

    router = make_router(tmp_path, evaluator=Stop())
    assert router.say("stop that").kind == "stopped"

    class Attach:
        def available(self):
            return True

        def evaluate(self, text, context=None):
            return EvalDecision("attach", "proj_a", 0.9, "follow up", "jev", work_item_id=wi)

    # wi is now stopped, so attach must be rejected (no resume).
    reply = router.say("follow up")
    assert reply.kind == "rejected"


def test_eval_ignore(tmp_path):
    class Ign:
        def available(self):
            return True

        def evaluate(self, text, context=None):
            return EvalDecision("ignore", None, 0.9, "not work", "generative")

    reply = make_router(tmp_path, evaluator=Ign()).say("do nothing")
    assert reply.kind == "ignored"


def test_eval_proposes_unknown_project_is_clarified(tmp_path):
    class Bad:
        def available(self):
            return True

        def evaluate(self, text, context=None):
            return EvalDecision("accept", "nope", 0.99, "bad project", "jev")

    reply = make_router(tmp_path, evaluator=Bad()).say("do it")
    assert reply.kind == "clarify"
    assert "not allowed" in reply.detail


def test_unavailable_evaluator_falls_back_to_clarify(tmp_path):
    class Off:
        def available(self):
            return False

        def evaluate(self, text, context=None):
            raise AssertionError("must not be called")

    reply = make_router(tmp_path, evaluator=Off()).say("analyze this")
    assert reply.kind == "clarify"


def test_jev_evaluator_uses_console_endpoint(monkeypatch, tmp_path):
    calls = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "model": "jev-1.13-free",
                "answers": {
                    "action": {"type": "choice", "choice": "accept", "confidence": 0.88,
                               "probabilities": {"accept": 0.88, "ignore": 0.12},
                    "project": {"type": "choice", "choice": "proj_a", "confidence": 0.91,
                                "probabilities": {"proj_a": 0.91, "proj_b": 0.09},
                },
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        return Resp()

    monkeypatch.setenv("OPENCODE_API_KEY", "console-key")
    monkeypatch.setattr("router_runner.evaluator.httpx.post", fake_post)
    ev = JevEvaluator("jev-1.13-free")
    assert ev.available() is True
    decision = ev.evaluate("dock the peptide", {"projects": ["proj_a"]})
    assert decision.action == "accept"
    assert decision.confidence == 0.88
    assert decision.evaluator == "jev"
    assert calls["url"] == JevEvaluator.ENDPOINT
    assert calls["json"]["model"] == "jev-1.13-free"
    assert calls["headers"]["Authorization"] == "Bearer console-key"
    assert calls["json"]["questions"]["action"]["type"] == "choice"
    assert "accept" in calls["json"]["questions"]["action"]["criteria"]
    assert decision.project == "proj_a"


def test_make_evaluator_prefers_jev_when_configured(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_EVAL_JEV_MODEL", "jev-1.13-free")
    assert isinstance(make_evaluator(), JevEvaluator)
    monkeypatch.delenv("MAGNOLIA_EVAL_JEV_MODEL", raising=False)
    assert type(make_evaluator()).__name__ == "GenerativeEvaluator"


def _ingest(tmp_path):
    engine = agentd.get_engine(str(tmp_path / "agentd.db"), out_root=tmp_path / "runs")
    from router_runner.replay import _ingest as ingest
    from router_runner.router import Router
    return ingest(Router(engine, registry={}), "eval-wi")
