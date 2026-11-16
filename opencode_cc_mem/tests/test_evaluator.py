"""Tests for the LLM Eval stage (Jev Console + generative fallback).

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_evaluator.py -q
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.evaluator import (
    EVALUATOR_SYSTEM_PROMPT,
    EvalDecision,
    GenerativeEvaluator,
    JevEvaluator,
    make_evaluator,
)
from router_runner.router import Router


def _accepted_evaluator_capabilities():
    return {
        client: {"evaluator": {"accepted": True}}
        for client in ("claude-code", "codex", "vibe")
    }


class _ClientExecutor:
    def __init__(self, response=None):
        self.response = response or {
            "action": "ignore",
            "project": None,
            "confidence": 0.9,
            "reason": "not routed work",
        }
        self.calls = []

    def run(self, prompt, **kwargs):
        from campaign_runner.executors.contracts import RunOutcome

        self.calls.append((prompt, kwargs))
        return SimpleNamespace(
            final_text=__import__("json").dumps(self.response),
            outcome=RunOutcome("success"),
            usage={},
            attempts=1,
            duration_seconds=0.01,
            session_identity=None,
        )


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
                               "probabilities": {"accept": 0.88, "ignore": 0.12}},
                    "project": {"type": "choice", "choice": "proj_a", "confidence": 0.91,
                                "probabilities": {"proj_a": 0.91, "proj_b": 0.09}},
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


def test_evaluator_each_client_route_without_api_key(monkeypatch, tmp_path):
    from router_runner import evaluator as evaluator_module

    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    monkeypatch.setattr(
        evaluator_module.llm,
        "is_llm_available",
        lambda: (_ for _ in ()).throw(AssertionError("API availability is unrelated")),
    )
    monkeypatch.setattr(
        evaluator_module,
        "executor_for",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("availability must not construct an executor")
        ),
    )
    checked = []
    for model in (
        "claude-code/claude-haiku-5-5",
        "codex/gpt-5.6-luna",
        "vibe/devstral-2",
        "opencode-go/deepseek-v4.1-flash",
    ):
        evaluator = GenerativeEvaluator(
            model,
            project_dir=tmp_path,
            role_capabilities=_accepted_evaluator_capabilities(),
            executable_available=lambda path: checked.append(path) or True,
        )
        assert evaluator.available() is True
    assert len(checked) == 4
    assert not (tmp_path / ".magnolia").exists()


def test_evaluator_availability_is_pure_and_checked_once(monkeypatch, tmp_path):
    from router_runner import evaluator as evaluator_module

    executor = _ClientExecutor()
    monkeypatch.setattr(
        evaluator_module,
        "executor_for",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("availability must not prepare a launch")
        ),
    )
    checks = []
    pure = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        governor_factory=lambda: (_ for _ in ()).throw(
            AssertionError("availability must not acquire a governor or memory service")
        ),
        project_dir=tmp_path,
        role_capabilities=_accepted_evaluator_capabilities(),
        executable_available=lambda _path: True,
    )
    assert pure.available() is True
    assert not (tmp_path / ".magnolia").exists()

    monkeypatch.setattr(evaluator_module, "executor_for", lambda *args: executor)
    evaluator = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        project_dir=tmp_path,
        role_capabilities=_accepted_evaluator_capabilities(),
        executable_available=lambda path: checks.append(path) or True,
    )
    router = make_router(tmp_path, evaluator=evaluator)

    assert router.say("triage this").kind == "ignored"
    assert len(checks) == 1
    assert executor.calls


def test_evaluator_refused_capability_is_unavailable(monkeypatch, tmp_path):
    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr(
        evaluator_module,
        "executor_for",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("refused capability must not fall back to OpenCode")
        ),
    )
    evaluator = GenerativeEvaluator(
        "codex/gpt-5.6-luna",
        project_dir=tmp_path,
        role_capabilities={"codex": {"evaluator": {"accepted": False}}},
        executable_available=lambda _path: True,
    )

    assert evaluator.available() is False
    assert evaluator.evaluate("triage this", {"projects": ["proj_a"]}) is None


def test_evaluator_selected_model_and_no_tools_one_turn(monkeypatch, tmp_path):
    from router_runner import evaluator as evaluator_module

    executor = _ClientExecutor()
    captured = {}

    def fake_executor(route, role, context):
        captured.update(route=route, role=role, context=context)
        return executor

    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    monkeypatch.setattr(evaluator_module, "executor_for", fake_executor)
    monkeypatch.delenv("PYTHON", raising=False)
    evaluator = GenerativeEvaluator(
        "claude-code/claude-haiku-5-5",
        project_dir=tmp_path,
        role_capabilities=_accepted_evaluator_capabilities(),
    )
    assert evaluator.evaluate("triage this", {"projects": ["proj_a"]}).action == "ignore"
    assert captured["route"].model == "claude-haiku-5-5"
    assert captured["role"] == "evaluator"
    assert captured["context"].role_cap == 1
    assert captured["context"].required_capabilities == ()
    assert captured["context"].python_executable == sys.executable
    assert executor.calls[0][1]["retries"] == 0


def test_executable_probe_uses_injected_lookup(monkeypatch):
    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr(evaluator_module.subprocess, "run", lambda *args, **kwargs: None)

    assert evaluator_module._executable_available(
        "missing-client", which=lambda _name: None
    ) is False
    assert evaluator_module._executable_available(
        "fixture-client", which=lambda name: f"/bin/{name}"
    ) is True


def test_evaluator_api_fallback_unchanged(monkeypatch):
    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr(evaluator_module.llm, "is_llm_available", lambda: True)
    monkeypatch.setattr(
        evaluator_module.llm,
        "call_llm_json",
        lambda *args, **kwargs: {
            "action": "accept",
            "project": "proj_a",
            "confidence": 0.91,
            "reason": "API decision",
        },
    )
    monkeypatch.setattr(
        evaluator_module,
        "executor_for",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("registry not used")),
    )
    decision = GenerativeEvaluator("openai/gpt-api-model").evaluate(
        "triage this", {"projects": ["proj_a"]}
    )
    assert decision is not None and decision.action == "accept"
    assert decision.project == "proj_a"


def test_jev_evaluator_unchanged(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_EVAL_JEV_MODEL", "jev-1.13-free")
    evaluator = make_evaluator()
    assert isinstance(evaluator, JevEvaluator)
    assert evaluator.model == "jev-1.13-free"


def test_go_generative_path_parses_json_with_preamble(monkeypatch):
    from router_runner import evaluator as evaluator_module

    class Out:
        returncode = 0
        stdout = '> build · deepseek-v4.1-flash\n{"action":"accept","project":"proj_a","confidence":0.92,"reason":"dock"}'
        stderr = ""

    monkeypatch.setattr("router_runner.evaluator.subprocess.run",
                        lambda *a, **k: Out())
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    ev = GenerativeEvaluator("opencode-go/deepseek-v4.1-flash")
    d = ev.evaluate("dock the peptide", {"projects": ["proj_a"]})
    assert d is not None
    assert d.action == "accept"
    assert d.project == "proj_a"
    assert d.confidence == 0.92
    assert d.evaluator == "generative"


def test_go_generative_path_returns_none_on_bad_json(monkeypatch):
    from router_runner import evaluator as evaluator_module

    class Out:
        returncode = 0
        stdout = "no json here"
        stderr = ""

    monkeypatch.setattr("router_runner.evaluator.subprocess.run",
                        lambda *a, **k: Out())
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    ev = GenerativeEvaluator("opencode-go/deepseek-v4.1-flash")
    assert ev.evaluate("dock", {}) is None


def test_go_generative_path_receives_system_prompt_and_closed_stdin(monkeypatch):
    seen = {}

    class Out:
        returncode = 0
        stdout = '{"action":"ignore","confidence":0.9,"reason":"x"}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["stdin"] = kwargs.get("stdin")
        return Out()

    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr("router_runner.evaluator.subprocess.run", fake_run)
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    decision = GenerativeEvaluator("opencode-go/deepseek-v4.1-flash").evaluate("dock", {})

    assert decision is not None
    assert EVALUATOR_SYSTEM_PROMPT in seen["cmd"][-1]
    assert seen["stdin"] is subprocess.DEVNULL


def test_generic_generative_path_receives_shared_system_prompt(monkeypatch):
    seen = {}

    monkeypatch.setattr("router_runner.evaluator.llm.is_llm_available", lambda: True)

    def fake_call(system, user, **kwargs):
        seen["system"] = system
        return {"action": "ignore", "confidence": 0.9, "reason": "x"}

    monkeypatch.setattr("router_runner.evaluator.llm.call_llm_json", fake_call)
    decision = GenerativeEvaluator("openai/gpt-test").evaluate("dock", {})

    assert decision is not None
    assert seen["system"] == EVALUATOR_SYSTEM_PROMPT


def test_go_generative_path_rejects_invalid_action(monkeypatch):
    class Out:
        returncode = 0
        stdout = '{"action":"launch","confidence":0.9,"reason":"x"}'
        stderr = ""

    from router_runner import evaluator as evaluator_module

    monkeypatch.setattr("router_runner.evaluator.subprocess.run", lambda *a, **k: Out())
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    assert GenerativeEvaluator("opencode-go/deepseek-v4.1-flash").evaluate("dock", {}) is None


def _ingest(tmp_path):
    engine = agentd.get_engine(str(tmp_path / "agentd.db"), out_root=tmp_path / "runs")
    from router_runner.replay import _ingest as ingest
    from router_runner.router import Router
    return ingest(Router(engine, registry={}), "eval-wi")


# Observed on OpenCode 2.3.0: a CLI call with neither --standalone nor --server
# starts a shared background service that outlives the call; once it runs, the
# OPENCODE_DB a later caller sets is ignored. The evaluator's one-shot calls
# set OPENCODE_DB, so they pass --standalone (a private server per call).

def _capture_run(monkeypatch, stdout="{}"):
    from router_runner import evaluator as evaluator_module

    seen = []

    class Out:
        returncode = 0
        stderr = ""

    Out.stdout = stdout

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs.get("env") or {}))
        return Out()

    monkeypatch.setattr("router_runner.evaluator.subprocess.run", fake_run)
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    return seen


def test_go_generative_run_is_standalone(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2_BIN", "/opt/oc/bin/opencode")
    seen = _capture_run(monkeypatch, '{"action":"ignore","confidence":0.9,"reason":"x"}')
    GenerativeEvaluator("opencode-go/deepseek-v4.1-flash").evaluate("dock", {})
    cmd, env = seen[0]
    assert cmd[:3] == ["/opt/oc/bin/opencode", "--standalone", "run"]
    assert env.get("OPENCODE_DB", "").endswith("opencode-v2.db")


def test_catalog_check_is_standalone(monkeypatch):
    from router_runner.evaluator import _catalog_has_model

    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2_BIN", "/opt/oc/bin/opencode")
    seen = _capture_run(monkeypatch, '{"data":[{"id":"jev-1.13-free"}]}')
    assert _catalog_has_model("jev-1.13-free") is True
    cmd, _env = seen[0]
    assert cmd == ["/opt/oc/bin/opencode", "--standalone", "api", "get", "/api/model"]


def test_evaluator_standalone_db_and_plain_stdout(monkeypatch, tmp_path):
    from router_runner import evaluator as evaluator_module

    calls = []

    class Out:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "/api/model" in argv:
            return Out('{"data":[{"id":"jev-1.13-free"}]}')
        return Out(
            '> build · model\nnoise\n'
            '{"action":"accept","project":"proj_a","confidence":0.92,'
            '"reason":"dock","meta":{"nested":true}}\ntrailer'
        )

    monkeypatch.setattr(evaluator_module.os, "environ", {"BASE_ENV": "ignored"})
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2_BIN", str(tmp_path / "opencode"))
    monkeypatch.setattr(evaluator_module, "_executable_available", lambda _path: True)
    monkeypatch.setattr(evaluator_module.llm, "is_llm_available", lambda: True)
    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    decision = GenerativeEvaluator("opencode-go/deepseek-v4.1-flash").evaluate(
        "dock", {"projects": ["proj_a"]}
    )
    assert decision is not None and decision.action == "accept"
    assert evaluator_module._catalog_has_model("jev-1.13-free") is True
    snapshot = __import__("json").loads(
        (Path(__file__).parent / "snapshots" / "opencode" / "evaluator.json").read_text()
    )
    observed = {
        "run_argv": [item.replace(str(tmp_path), "@@TMP@@") for item in calls[0][0]],
        "catalog_argv": [item.replace(str(tmp_path), "@@TMP@@") for item in calls[1][0]],
        "environment_keys": sorted(calls[0][1]["env"]),
        "step_cap_config": calls[0][1]["env"].get("OPENCODE_CONFIG_CONTENT"),
        "run_timeout_seconds": calls[0][1]["timeout"],
        "catalog_timeout_seconds": calls[1][1]["timeout"],
        "agent_file_bytes": None,
    }
    assert observed == snapshot
