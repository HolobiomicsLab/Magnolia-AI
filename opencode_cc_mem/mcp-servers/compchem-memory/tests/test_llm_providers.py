"""Provider-aware LLM client: resolution order, per-provider defaults,
backward compat, error containment, and HTTP shapes."""
import json
import os
import sys
from unittest.mock import MagicMock

import httpx
import pytest

# Reload the module fresh per test so module-level state can't leak.
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear every env var the resolver looks at, before each test."""
    for k in [
        "MAGNOLIA_LLM_PROVIDER",
        "MAGNOLIA_LLM_API_KEY",
        "MAGNOLIA_LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ]:
        monkeypatch.delenv(k, raising=False)


from compchem_memory import llm  # noqa: E402


# ============ _resolve_provider =============

def test_resolve_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    assert llm._resolve_provider() == "openai"


def test_resolve_explicit_override_case_insensitive(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_LLM_PROVIDER", "DeepSeek")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    assert llm._resolve_provider() == "deepseek"


def test_resolve_explicit_override_invalid_falls_through(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_LLM_PROVIDER", "bogus")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    assert llm._resolve_provider() == "deepseek"


def test_resolve_backward_compat_magnolia_key_means_anthropic(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_LLM_API_KEY", "legacy-anthropic")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    assert llm._resolve_provider() == "anthropic"


def test_resolve_autodetect_deepseek_first(monkeypatch):
    """DeepSeek wins over Anthropic and OpenAI in autodetect (DeepSeek default)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an")
    monkeypatch.setenv("OPENAI_API_KEY", "op")
    assert llm._resolve_provider() == "deepseek"


def test_resolve_autodetect_anthropic_when_only_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an")
    assert llm._resolve_provider() == "anthropic"


def test_resolve_autodetect_openai_when_only_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "op")
    assert llm._resolve_provider() == "openai"


def test_resolve_returns_none_when_no_keys():
    assert llm._resolve_provider() is None


# ============ _get_api_key =============

def test_get_api_key_anthropic_prefers_magnolia_var(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_LLM_API_KEY", "magnolia-an")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "raw-an")
    assert llm._get_api_key("anthropic") == "magnolia-an"


def test_get_api_key_anthropic_falls_back_to_anthropic_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "raw-an")
    assert llm._get_api_key("anthropic") == "raw-an"


def test_get_api_key_deepseek(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    assert llm._get_api_key("deepseek") == "ds"


def test_get_api_key_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "op")
    assert llm._get_api_key("openai") == "op"


# ============ _get_model =============

def test_get_model_defaults(monkeypatch):
    assert llm._get_model("deepseek") == "deepseek-chat"
    assert llm._get_model("anthropic") == "claude-haiku-4-5-20251001"
    assert llm._get_model("openai") == "gpt-4o-mini"


def test_get_model_env_override_applies_to_all_providers(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_LLM_MODEL", "deepseek-reasoner")
    assert llm._get_model("deepseek") == "deepseek-reasoner"
    assert llm._get_model("anthropic") == "deepseek-reasoner"  # caller's responsibility to use sensibly


# ============ _get_base_url =============

def test_get_base_url_default_deepseek():
    assert llm._get_base_url("deepseek") == "https://api.deepseek.com/v1"


def test_get_base_url_default_openai():
    assert llm._get_base_url("openai") == "https://api.openai.com/v1"


def test_get_base_url_env_override_with_v1_appends_nothing(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://custom.example.com/v1")
    assert llm._get_base_url("deepseek") == "https://custom.example.com/v1"


def test_get_base_url_env_override_without_v1_appends(monkeypatch):
    """Critical: many DeepSeek users set DEEPSEEK_BASE_URL=https://api.deepseek.com (no /v1)."""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    assert llm._get_base_url("deepseek") == "https://api.deepseek.com/v1"


def test_get_base_url_env_override_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://oai-proxy.example.com/")
    assert llm._get_base_url("openai") == "https://oai-proxy.example.com/v1"


# ============ is_llm_available =============

def test_is_llm_available_true_when_deepseek_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    assert llm.is_llm_available() is True


def test_is_llm_available_false_when_no_keys():
    assert llm.is_llm_available() is False


# ============ call_llm: error containment =============

def test_call_llm_returns_none_when_no_provider():
    assert llm.call_llm("sys", "user") is None


def test_call_llm_returns_none_when_httpx_raises(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    def boom(*a, **kw):
        raise httpx.ConnectError("simulated network failure")
    monkeypatch.setattr(llm.httpx, "post", boom)
    assert llm.call_llm("sys", "user") is None


def test_call_llm_returns_none_when_anthropic_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an")
    # Replace _call_anthropic to raise — avoids needing a real Anthropic mock.
    def boom(*a, **kw):
        raise RuntimeError("simulated SDK failure")
    monkeypatch.setattr(llm, "_call_anthropic", boom)
    assert llm.call_llm("sys", "user") is None


# ============ call_llm: DeepSeek HTTP shape =============

def test_call_llm_deepseek_posts_to_chat_completions(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key-abc")
    captured = {}
    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"choices": [{"message": {"content": "from deepseek"}}]}
        return resp
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.call_llm("you are a helper", "what is 2+2?", max_tokens=42)
    assert out == "from deepseek"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer ds-key-abc"
    assert captured["json"]["model"] == "deepseek-chat"
    assert captured["json"]["max_tokens"] == 42
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "you are a helper"},
        {"role": "user", "content": "what is 2+2?"},
    ]


def test_call_llm_openai_posts_to_default_base(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "op-key")
    captured = {}
    def fake_post(url, **kw):
        captured["url"] = url
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"choices": [{"message": {"content": "ok"}}]}
        return resp
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    llm.call_llm("s", "u")
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"


def test_call_llm_deepseek_uses_custom_base_url(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")  # no /v1
    captured = {}
    def fake_post(url, **kw):
        captured["url"] = url
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"choices": [{"message": {"content": "ok"}}]}
        return resp
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    llm.call_llm("s", "u")
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"


def test_call_llm_returns_none_on_empty_choices(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"choices": []}
        return resp
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.call_llm("s", "u") is None


# ============ call_llm_json =============

def test_call_llm_json_strips_code_fence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]}
        return resp
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.call_llm_json("s", "u") == {"a": 1}


def test_call_llm_json_returns_none_on_parse_failure(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"choices": [{"message": {"content": "not json at all"}}]}
        return resp
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.call_llm_json("s", "u") is None


def test_call_llm_json_returns_none_when_text_is_none(monkeypatch):
    # No provider → call_llm returns None → call_llm_json must too
    assert llm.call_llm_json("s", "u") is None
