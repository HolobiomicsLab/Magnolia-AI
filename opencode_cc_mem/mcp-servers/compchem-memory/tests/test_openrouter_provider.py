"""OpenRouter provider support tests.

Run from mcp-servers/compchem-memory/:
    python3 -m pytest tests/test_openrouter_provider.py -q
"""

from __future__ import annotations

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
    ):
        monkeypatch.delenv(var, raising=False)


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
