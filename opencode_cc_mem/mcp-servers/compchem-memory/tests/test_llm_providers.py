"""Provider-aware LLM client: resolution order, per-provider defaults,
backward compat, error containment, and HTTP shapes."""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

# Reload the module fresh per test so module-level state can't leak.
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear every env var the resolver looks at, before each test."""
    for k in [
        "MAGNOLIA_MEMORY_PROVIDER",
        "MAGNOLIA_MEMORY_MODEL",
        "MAGNOLIA_LLM_PROVIDER",   # deprecated alias, still read
        "MAGNOLIA_LLM_MODEL",      # deprecated alias, still read
        "MAGNOLIA_LLM_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "OPENAI_API_KEY",
        "MAGNOLIA_OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "KIMI_PLAN_MAGNOLIA_API_KEY",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "MISTRAL_API_KEY",
        "MAGNOLIA_MISTRAL_API_KEY",
        "MISTRAL_BASE_URL",
        "ZHIPU_API_KEY",
        "ZAI_API_KEY",
        "ZAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "MAGNOLIA_OPENROUTER_PROVIDER",
        "OPENROUTER_CODING_API_KEY",
        "MAGNOLIA_CRITIC_API_KEY",
        "MAGNOLIA_CRITIC_PROVIDER",
        "MAGNOLIA_CRITIC_MODEL",
        "MAGNOLIA_CRITIC_BASE_URL",
    ]:
        monkeypatch.delenv(k, raising=False)


from compchem_memory import llm  # noqa: E402


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"prompt_tokens": 12508, "completion_tokens": 31, "cost": 0.00184,
             "prompt_tokens_details": {"cached_tokens": 12416, "cache_write_tokens": 7}},
            {"prompt_tokens": 12508, "cached_tokens": 12416, "cache_write_tokens": 7,
             "completion_tokens": 31, "cost": 0.00184},
        ),
        (
            {"prompt_tokens": 80, "completion_tokens": 9,
             "prompt_cache_hit_tokens": 60},
            {"prompt_tokens": 80, "cached_tokens": 60, "completion_tokens": 9},
        ),
        (
            SimpleNamespace(input_tokens=10, cache_read_input_tokens=3,
                            cache_creation_input_tokens=2, output_tokens=4),
            {"prompt_tokens": 15, "cached_tokens": 3, "cache_write_tokens": 2,
             "completion_tokens": 4},
        ),
        (
            {"input_tokens": 10, "cache_read_input_tokens": 3,
             "cache_creation_input_tokens": 2, "output_tokens": 4},
            {"prompt_tokens": 15, "cached_tokens": 3, "cache_write_tokens": 2,
             "completion_tokens": 4},
        ),
        (None, None),
        ({"prompt_tokens": "many", "prompt_tokens_details": []}, None),
    ],
)
def test_normalise_usage_shapes_and_malformed_values(usage, expected):
    assert llm.normalise_usage(usage) == expected


# ============ _resolve_provider =============

def test_resolve_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    assert llm._resolve_provider() == "openai"


def test_resolve_explicit_override_case_insensitive(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "DeepSeek")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    assert llm._resolve_provider() == "deepseek"


def test_resolve_explicit_override_invalid_falls_through(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "bogus")
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


# ============ model-name prefix routing (_resolve_call) =============

def test_provider_for_model_prefixes():
    assert llm._provider_for_model("deepseek-flash") == "deepseek"
    assert llm._provider_for_model("DeepSeek-V4-Pro") == "deepseek"   # case-insensitive
    assert llm._provider_for_model("claude-haiku-4-5-20251001") == "anthropic"
    assert llm._provider_for_model("gpt-5-mini") == "openai"
    assert llm._provider_for_model("o4-mini") == "openai"
    assert llm._provider_for_model("kimi-k2.5") == "kimi"
    assert llm._provider_for_model("k3") == "kimi"
    assert llm._provider_for_model("llama-3-local") is None


def test_resolve_call_model_prefix_beats_key_autodetect(monkeypatch):
    """A claude-* model name must route to Anthropic even with a DeepSeek key."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "claude-haiku-4-5-20251001")
    assert llm._resolve_call() == ("anthropic", "claude-haiku-4-5-20251001")


def test_resolve_call_explicit_provider_beats_model_prefix(monkeypatch):
    """Proxy case: an OpenAI-compatible endpoint serving a deepseek-named model."""
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openai")
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "deepseek-flash")
    assert llm._resolve_call() == ("openai", "deepseek-flash")


def test_resolve_call_unknown_model_falls_back_to_key_autodetect(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "my-private-finetune")
    assert llm._resolve_call() == ("deepseek", "my-private-finetune")


def test_resolve_call_no_model_uses_provider_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    assert llm._resolve_call() == ("deepseek", "deepseek-flash")


def test_resolve_call_placeholder_model_ignored(monkeypatch):
    """The 2026-08 freeze: an unrendered @@...@@ model must act as unset
    (exercised via the legacy env name — it reads through the same guard)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("MAGNOLIA_LLM_MODEL", "@@DISTILL_MODEL@@")
    assert llm._resolve_call() == ("deepseek", "deepseek-flash")


def test_resolve_call_legacy_env_names_still_honored(monkeypatch):
    """Old MAGNOLIA_LLM_MODEL/MAGNOLIA_LLM_PROVIDER keep working (warn once)
    so pre-rename rendered configs don't break before the next re-render."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("MAGNOLIA_LLM_MODEL", "kimi-k2.5")
    assert llm._resolve_call() == ("kimi", "kimi-k2.5")


def test_resolve_call_nothing_configured(monkeypatch):
    assert llm._resolve_call() == (None, None)


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


@pytest.mark.parametrize(
    ("provider", "alias", "generic"),
    [
        ("openai", "MAGNOLIA_OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("mistral", "MAGNOLIA_MISTRAL_API_KEY", "MISTRAL_API_KEY"),
    ],
)
def test_memory_route_prefers_magnolia_provider_key(
    monkeypatch, provider, alias, generic
):
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", provider)
    monkeypatch.setenv(alias, "magnolia-key")
    monkeypatch.setenv(generic, "client-key")

    route = llm.resolve_route("memory")

    assert route is not None
    assert route.key == "magnolia-key"


@pytest.mark.parametrize(
    ("provider", "alias", "generic"),
    [
        ("openai", "MAGNOLIA_OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("mistral", "MAGNOLIA_MISTRAL_API_KEY", "MISTRAL_API_KEY"),
    ],
)
def test_critic_route_prefers_magnolia_provider_key(
    monkeypatch, provider, alias, generic
):
    monkeypatch.setenv("MAGNOLIA_CRITIC_PROVIDER", provider)
    monkeypatch.setenv(alias, "magnolia-key")
    monkeypatch.setenv(generic, "client-key")

    route = llm.resolve_route("critic")

    assert route is not None
    assert route.key == "magnolia-key"


@pytest.mark.parametrize(
    ("provider", "alias", "generic"),
    [
        ("openai", "MAGNOLIA_OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("mistral", "MAGNOLIA_MISTRAL_API_KEY", "MISTRAL_API_KEY"),
    ],
)
def test_memory_and_critic_routes_fall_back_to_generic_provider_key(
    monkeypatch, provider, alias, generic
):
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", provider)
    monkeypatch.setenv("MAGNOLIA_CRITIC_PROVIDER", provider)
    monkeypatch.delenv(alias, raising=False)
    monkeypatch.setenv(generic, "client-key")

    memory = llm.resolve_route("memory")
    critic = llm.resolve_route("critic")

    assert memory is not None and memory.key == "client-key"
    assert critic is not None and critic.key == "client-key"


@pytest.mark.parametrize(
    ("provider", "alias"),
    [
        ("openai", "MAGNOLIA_OPENAI_API_KEY"),
        ("mistral", "MAGNOLIA_MISTRAL_API_KEY"),
    ],
)
def test_memory_and_critic_routes_autodetect_magnolia_provider_key(
    monkeypatch, provider, alias
):
    monkeypatch.setenv(alias, "magnolia-key")

    memory = llm.resolve_route("memory")
    critic = llm.resolve_route("critic")

    assert memory is not None and memory.provider == provider
    assert critic is not None and critic.provider == provider


# ============ _get_model =============

def test_get_model_defaults(monkeypatch):
    assert llm._get_model("deepseek") == "deepseek-flash"
    assert llm._get_model("anthropic") == "claude-haiku-4-5-20251001"
    assert llm._get_model("openai") == "gpt-4o-mini"


def test_get_model_env_override_applies_to_all_providers(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "deepseek-reasoner")
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
    assert captured["json"]["model"] == "deepseek-flash"
    assert captured["json"]["max_tokens"] == 42
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "you are a helper"},
        {"role": "user", "content": "what is 2+2?"},
    ]


def test_call_llm_returns_finish_reason_when_requested(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"choices": [{"message": {"content": "cut here"},
                                          "finish_reason": "length"}]}
        return resp
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.call_llm("s", "u", return_finish_reason=True) == ("cut here", "length")
    assert llm.call_llm("s", "u") == "cut here"          # default stays text-only


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


def test_call_llm_json_passes_cache_prefix_through(monkeypatch):
    captured = {}

    def fake_call(*args, **kwargs):
        captured.update(kwargs)
        return ('{"ok": true}', "stop")

    monkeypatch.setattr(llm, "call_llm", fake_call)
    assert llm.call_llm_json("s", "transcript", cache_prefix="transcript") == {"ok": True}
    assert captured["cache_prefix"] == "transcript"


def test_disable_thinking_and_temperature_sent_for_deepseek(monkeypatch):
    """call_llm_json(disable_thinking=True, temperature=0) must send DeepSeek's
    thinking-disabled param and temperature in the request body."""
    from compchem_memory import llm
    monkeypatch.delenv("MAGNOLIA_MEMORY_PROVIDER", raising=False)
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
    monkeypatch.delenv("MAGNOLIA_MEMORY_PROVIDER", raising=False)
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
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "kimi")
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


# ============ P0.4: resolved-model identity =============

def _timing_rows(tmp_path):
    rows = (tmp_path / ".magnolia" / "llm-timing.jsonl").read_text().splitlines()
    return [json.loads(line) for line in rows]


def test_call_llm_records_resolved_model_deepseek(monkeypatch, tmp_path):
    """Requested id and provider-echoed id both land in telemetry (P0.4)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))

    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {
            "model": "deepseek-v4.1-flash",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100, "completion_tokens": 5, "cost": 0.25,
                "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 4},
            },
        }
        return resp

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    llm.call_llm("s", "u")
    row = _timing_rows(tmp_path)[-1]
    assert row["model"] == "deepseek-flash"
    assert row["resolved_model"] == "deepseek-v4.1-flash"
    assert row["outcome"] == "ok"
    assert {key: row[key] for key in (
        "prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens", "cost",
    )} == {
        "prompt_tokens": 100, "cached_tokens": 80, "cache_write_tokens": 4,
        "completion_tokens": 5, "cost": 0.25,
    }


def test_call_llm_records_resolved_model_kimi(monkeypatch, tmp_path):
    monkeypatch.setenv("KIMI_PLAN_MAGNOLIA_API_KEY", "km")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))

    def fake_post(url, **kw):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {
            "model": "k3-2026-08",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
        }
        return resp

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    llm.call_llm("s", "u")
    row = _timing_rows(tmp_path)[-1]
    assert row["model"] == "k3"
    assert row["resolved_model"] == "k3-2026-08"


def test_call_llm_resolved_model_none_on_error(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))

    def boom(*a, **kw):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(llm.httpx, "post", boom)
    assert llm.call_llm("s", "u") is None
    row = _timing_rows(tmp_path)[-1]
    assert row["outcome"] == "error"
    assert row["resolved_model"] is None


def test_call_anthropic_returns_resolved_model(monkeypatch):
    import anthropic

    class FakeMessage:
        content = [type("Block", (), {"text": "hello"})()]
        stop_reason = "end_turn"
        model = "claude-haiku-4-5-20251001"

    class FakeMessages:
        def create(self, **kwargs):
            return FakeMessage()

    class FakeAnthropic:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    out, finish, resolved, usage = llm._call_anthropic(
        "k", "claude-haiku-4-5", "s", "u", 10)
    assert (out, finish, resolved) == ("hello", "end_turn", "claude-haiku-4-5-20251001")
    assert usage is None


def test_call_anthropic_marks_reused_prefix(monkeypatch):
    import anthropic

    captured = {}

    class FakeMessage:
        content = [type("Block", (), {"text": "hello"})()]
        stop_reason = "end_turn"
        model = "claude-haiku-4-5-20251001"
        usage = None

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeMessage()

    class FakeAnthropic:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    llm._call_anthropic(
        "k", "claude-haiku-4-5", "s", "transcript plus instruction", 10,
        cache_prefix="transcript",
    )
    assert captured["messages"][0]["content"] == [
        {
            "type": "text", "text": "transcript",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": " plus instruction"},
    ]


# ============ mistral and zai providers =============

def _capture_openai_compat(monkeypatch, content="ok"):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json)
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"model": json["model"],
                             "choices": [{"message": {"content": content},
                                          "finish_reason": "stop"}]}
        return resp

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    return captured


@pytest.mark.parametrize("model", [
    "mistral-small-latest", "ministral-8b-latest", "magistral-medium-latest",
    "devstral-latest", "codestral-latest", "pixtral-large-latest",
    "open-mistral-nemo",
])
def test_provider_for_model_mistral_families(model):
    assert llm._provider_for_model(model) == "mistral"


def test_provider_for_model_glm_is_zai():
    assert llm._provider_for_model("glm-5.3-flash") == "zai"


def test_provider_for_model_vendor_slash_stays_openrouter():
    assert llm._provider_for_model("mistralai/mistral-small") == "openrouter"
    assert llm._provider_for_model("z-ai/glm-5.3") == "openrouter"


def test_resolve_autodetect_mistral_and_zai_never_hijack(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mk")
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    assert llm._resolve_provider() == "openrouter"


def test_resolve_autodetect_mistral_only(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mk")
    assert llm._resolve_call() == ("mistral", "mistral-small-latest")


def test_resolve_autodetect_zai_only_via_alias(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "zk")
    assert llm._resolve_call() == ("zai", "glm-5.3-flash")


def test_get_api_key_zai_prefers_zhipu_name(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu")
    monkeypatch.setenv("ZAI_API_KEY", "alias")
    assert llm._get_api_key("zai") == "zhipu"


def test_explicit_mistral_provider_with_foreign_model_id(monkeypatch):
    """Mistral also hosts other vendors' models; the explicit provider wins."""
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "mistral")
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "deepseek-v4-1-flash-1017")
    assert llm._resolve_call() == ("mistral", "deepseek-v4-1-flash-1017")


def test_zai_coding_plan_base_url_keeps_v4(monkeypatch):
    monkeypatch.setenv("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4/")
    assert llm._get_base_url("zai") == "https://api.z.ai/api/coding/paas/v4"


def test_call_llm_mistral_posts_chat_completions(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mk-123")
    captured = _capture_openai_compat(monkeypatch, "from mistral")
    assert llm.call_llm("sys", "user", disable_thinking=True) == "from mistral"
    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer mk-123"
    assert captured["json"]["model"] == "mistral-small-latest"
    assert "thinking" not in captured["json"]


def test_call_llm_zai_disables_thinking(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "zk-123")
    captured = _capture_openai_compat(monkeypatch, "from glm")
    assert llm.call_llm("sys", "user", disable_thinking=True) == "from glm"
    assert captured["url"] == "https://api.z.ai/api/paas/v4/chat/completions"
    assert captured["json"]["thinking"] == {"type": "disabled"}
