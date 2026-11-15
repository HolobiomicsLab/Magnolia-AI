"""OpenRouter provider support tests.

Run from mcp-servers/compchem-memory/:
    python3 -m pytest tests/test_openrouter_provider.py -q
"""

from __future__ import annotations

import json
import os

import pytest

from compchem_memory import llm


def _clear_provider_env(monkeypatch):
    for var in (
        "MAGNOLIA_MEMORY_PROVIDER", "MAGNOLIA_LLM_PROVIDER",
        "MAGNOLIA_MEMORY_MODEL", "MAGNOLIA_LLM_MODEL",
        "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY", "MAGNOLIA_LLM_API_KEY",
        "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
        "MAGNOLIA_OPENROUTER_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)
    llm._INVALID_OPENROUTER_PROVIDER_WARNED.clear()


def test_explicit_openrouter_resolution(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider, model = llm._resolve_call()
    assert provider == llm.PROVIDER_OPENROUTER
    assert model == "deepseek/deepseek-v4.1-flash"
    assert llm._get_base_url(provider) == "https://openrouter.ai/api/v1"
    assert llm._get_api_key(provider) == "sk-or-test"


def test_slash_model_autodetects_openrouter(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "zai/glm-5.3")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider, model = llm._resolve_call()
    assert provider == llm.PROVIDER_OPENROUTER
    assert model == "zai/glm-5.3"


def test_vendor_prefix_does_not_hijack_openrouter_id(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider, model = llm._resolve_call()
    assert provider == llm.PROVIDER_OPENROUTER
    assert model == "deepseek/deepseek-v4.1-flash"


def test_openrouter_default_model(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider, model = llm._resolve_call()
    assert provider == llm.PROVIDER_OPENROUTER
    assert model == "deepseek/deepseek-v4.1-flash"


def test_key_autodetect_openrouter(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider, _ = llm._resolve_call()
    assert provider == llm.PROVIDER_OPENROUTER


def test_openai_compat_call_uses_gateway(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "model": "deepseek/deepseek-v4.1-flash",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.call_llm("sys", "user", max_tokens=16)
    assert out == "OK"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    assert captured["headers"]["X-Title"] == "Magnolia"
    assert captured["headers"]["HTTP-Referer"] == "https://magnolia.local"
    assert captured["json"]["model"] == "deepseek/deepseek-v4.1-flash"


def test_base_url_override(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example.com/api")
    assert llm._get_base_url(llm.PROVIDER_OPENROUTER) == "https://proxy.example.com/api/v1"


def test_provider_routing_and_reasoning_switch_reach_both_request_builders(
    monkeypatch, tmp_path,
):
    _clear_provider_env(monkeypatch)
    provider = {"order": ["DeepInfra", "GMICloud"], "allow_fallbacks": True}
    monkeypatch.setenv("MAGNOLIA_OPENROUTER_PROVIDER", json.dumps(provider))
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    captured = []

    class FakeResp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "model": "deepseek/deepseek-v4.1-flash",
                "provider": "DeepInfra",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }

    def fake_post(url, **kwargs):
        captured.append(kwargs["json"])
        return FakeResp()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    route = llm.resolve_route("memory")
    assert route is not None
    result = llm.call_route(route, "sys", "user", max_tokens=16, disable_thinking=True)
    assert result.upstream_provider == "DeepInfra"
    assert llm.call_llm("sys", "user", max_tokens=16, disable_thinking=True) == "OK"

    expected = {
        "model": "deepseek/deepseek-v4.1-flash", "max_tokens": 16,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ],
        "provider": provider, "reasoning": {"enabled": False},
    }
    assert captured == [expected, expected]
    timing = json.loads(
        (tmp_path / ".magnolia" / "llm-timing.jsonl").read_text().splitlines()[-1]
    )
    assert timing["upstream_provider"] == "DeepInfra"


def test_reasoning_400_retries_once_without_reasoning(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    bodies = []

    class FakeResp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError("retry did not recover")

        def json(self):
            return {
                "model": "vendor/model", "provider": "DeepInfra",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }

    def fake_post(url, **kwargs):
        bodies.append(kwargs["json"])
        return FakeResp(400, "reasoning is unsupported") if len(bodies) % 2 else FakeResp(200)

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    route = llm.resolve_route("memory")
    assert route is not None
    assert llm.call_route(route, "s", "u", max_tokens=1, disable_thinking=True).text == "OK"
    assert llm.call_llm("s", "u", max_tokens=1, disable_thinking=True) == "OK"
    assert len(bodies) == 4
    assert "reasoning" in bodies[0] and "reasoning" not in bodies[1]
    assert "reasoning" in bodies[2] and "reasoning" not in bodies[3]


def test_invalid_provider_json_is_ignored_with_one_stderr_line(monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_OPENROUTER_PROVIDER", "not-json")
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key", "openai",
    )

    first = llm._route_request(route, "s", "u", 1, None, False, False)[1]
    second = llm._route_request(route, "s", "u", 1, None, False, False)[1]

    assert "provider" not in first and "provider" not in second
    assert capsys.readouterr().err.count("MAGNOLIA_OPENROUTER_PROVIDER") == 1
