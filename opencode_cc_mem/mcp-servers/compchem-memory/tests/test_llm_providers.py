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
        "KIMI_PLAN_MAGNOLIA_API_KEY",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
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
    assert llm._get_model("deepseek") == "deepseek-v4-flash"
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
    assert captured["json"]["model"] == "deepseek-v4-flash"
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


def test_disable_thinking_and_temperature_sent_for_deepseek(monkeypatch):
    """call_llm_json(disable_thinking=True, temperature=0) must send DeepSeek's
    thinking-disabled param and temperature in the request body."""
    from compchem_memory import llm
    monkeypatch.delenv("MAGNOLIA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MAGNOLIA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    captured = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "{\"ok\": 1}"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return FakeResp()

    monkeypatch.setattr(llm.httpx, "post", fake_post)

    out = llm.call_llm_json("sys", "user", disable_thinking=True, temperature=0)
    assert out == {"ok": 1}
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["temperature"] == 0


def test_thinking_not_sent_by_default(monkeypatch):
    from compchem_memory import llm
    monkeypatch.delenv("MAGNOLIA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MAGNOLIA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    captured = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(llm.httpx, "post",
                        lambda url, headers=None, json=None, timeout=None: captured.update(body=json) or FakeResp())
    llm.call_llm_json("sys", "user")
    assert "thinking" not in captured["body"]
    assert "temperature" not in captured["body"]



# ============ kimi provider =============

def test_resolve_explicit_kimi(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_LLM_PROVIDER", "kimi")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    assert llm._resolve_provider() == "kimi"


def test_resolve_autodetect_kimi_when_only_kimi_key(monkeypatch):
    monkeypatch.setenv("KIMI_PLAN_MAGNOLIA_API_KEY", "km")
    assert llm._resolve_provider() == "kimi"


def test_resolve_autodetect_kimi_via_generic_key(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "kk")
    assert llm._resolve_provider() == "kimi"


def test_resolve_autodetect_kimi_is_last(monkeypatch):
    """A Kimi key alone never hijacks an existing deepseek/anthropic/openai setup."""
    monkeypatch.setenv("KIMI_PLAN_MAGNOLIA_API_KEY", "km")
    monkeypatch.setenv("OPENAI_API_KEY", "op")
    assert llm._resolve_provider() == "openai"


def test_get_api_key_kimi_prefers_plan_magnolia_var(monkeypatch):
    monkeypatch.setenv("KIMI_PLAN_MAGNOLIA_API_KEY", "km-plan")
    monkeypatch.setenv("KIMI_API_KEY", "kk-generic")
    assert llm._get_api_key("kimi") == "km-plan"


def test_get_api_key_kimi_falls_back_to_generic_var(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "kk-generic")
    assert llm._get_api_key("kimi") == "kk-generic"


def test_get_model_default_kimi():
    assert llm._get_model("kimi") == "k3"


def test_get_base_url_default_kimi():
    assert llm._get_base_url("kimi") == "https://api.kimi.com/coding/v1"


def test_get_base_url_kimi_env_override_without_v1_appends(monkeypatch):
    monkeypatch.setenv("KIMI_BASE_URL", "https://kimi-proxy.example.com/coding")
    assert llm._get_base_url("kimi") == "https://kimi-proxy.example.com/coding/v1"


def test_call_llm_kimi_posts_anthropic_messages_shape(monkeypatch):
    """Kimi-for-Coding speaks the Anthropic Messages schema, not chat/completions."""
    monkeypatch.setenv("KIMI_PLAN_MAGNOLIA_API_KEY", "km-key-abc")
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"content": [{"type": "text", "text": "from kimi"}]}
        return resp

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.call_llm("you are a helper", "what is 2+2?", max_tokens=42)
    assert out == "from kimi"
    assert captured["url"] == "https://api.kimi.com/coding/v1/messages"
    assert captured["headers"]["x-api-key"] == "km-key-abc"
    assert captured["headers"]["Authorization"] == "Bearer km-key-abc"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["json"]["model"] == "k3"
    assert captured["json"]["max_tokens"] == 42
    assert captured["json"]["thinking"] == {"type": "disabled"}  # always off: small budgets
    assert captured["json"]["system"] == "you are a helper"
    assert captured["json"]["messages"] == [{"role": "user", "content": "what is 2+2?"}]


def test_call_llm_kimi_skips_non_text_blocks(monkeypatch):
    """Thinking models may lead with a thinking block; the first text block wins."""
    monkeypatch.setenv("KIMI_PLAN_MAGNOLIA_API_KEY", "km")

    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "real answer"},
        ]}
        return resp

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.call_llm("s", "u") == "real answer"


def test_call_llm_kimi_returns_none_when_no_text_block(monkeypatch):
    monkeypatch.setenv("KIMI_PLAN_MAGNOLIA_API_KEY", "km")

    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"content": []}
        return resp

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.call_llm("s", "u") is None
