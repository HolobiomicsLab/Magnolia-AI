"""LLM integration: provider-aware (DeepSeek / Anthropic / OpenAI).

Magnolia uses the LLM for memory extraction, retrieval re-ranking,
compaction, and (via the magnolia wrapper) GOAL.md scaffolding. Until
2026-05-30 this module was hardcoded to Anthropic, which silently
failed when the operator only had DeepSeek / OpenAI keys configured —
the heuristic fallback then flooded staging with content-free entries
(see commit cab67c0). This rewrite makes the provider negotiable.

## Resolution order

1. `MAGNOLIA_LLM_PROVIDER` (`deepseek`/`anthropic`/`openai`) — explicit override.
2. `MAGNOLIA_LLM_API_KEY` set → `anthropic` (backward-compat: this was the
   old hardcoded path, and an operator who deliberately set it likely meant
   "use this Anthropic key").
3. Autodetect by first key present, in order: `DEEPSEEK_API_KEY` →
   `ANTHROPIC_API_KEY` → `OPENAI_API_KEY`. DeepSeek-first is intentional:
   it's the cheapest and the most commonly-configured key in the Magnolia
   user environment.

## Configuration

- `MAGNOLIA_LLM_MODEL` overrides the per-provider default model.
- `DEEPSEEK_BASE_URL` / `OPENAI_BASE_URL` override the API base URL; `/v1`
  is appended automatically if missing (DeepSeek's docs show both forms).
- All errors are swallowed and surface as `None` — callers must handle the
  None case. (The heuristic extractor and prompt-fallback in the wrapper
  both rely on this contract.)
"""

import json
import os
import re

import httpx

PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"

_VALID_PROVIDERS = {PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC, PROVIDER_OPENAI}

_DEFAULT_MODEL = {
    # deepseek-chat is a back-compat alias for deepseek-v4-flash, deprecated
    # 2026-07-24; use the explicit v4 id (1M context, cheap). Override per
    # provider via MAGNOLIA_LLM_MODEL (e.g. deepseek-v4-pro for higher recall).
    PROVIDER_DEEPSEEK: "deepseek-v4-flash",
    PROVIDER_ANTHROPIC: "claude-haiku-4-5-20251001",
    PROVIDER_OPENAI: "gpt-4o-mini",
}

_DEFAULT_BASE = {
    PROVIDER_DEEPSEEK: "https://api.deepseek.com/v1",
    PROVIDER_OPENAI: "https://api.openai.com/v1",
}

_BASE_URL_ENV = {
    PROVIDER_DEEPSEEK: "DEEPSEEK_BASE_URL",
    PROVIDER_OPENAI: "OPENAI_BASE_URL",
}


def _resolve_provider() -> str | None:
    """Pick a provider per the order documented at module top.
    Returns None if no usable provider is configured."""
    explicit = (os.environ.get("MAGNOLIA_LLM_PROVIDER") or "").strip().lower()
    if explicit in _VALID_PROVIDERS:
        return explicit
    if os.environ.get("MAGNOLIA_LLM_API_KEY"):
        return PROVIDER_ANTHROPIC
    if os.environ.get("DEEPSEEK_API_KEY"):
        return PROVIDER_DEEPSEEK
    if os.environ.get("ANTHROPIC_API_KEY"):
        return PROVIDER_ANTHROPIC
    if os.environ.get("OPENAI_API_KEY"):
        return PROVIDER_OPENAI
    return None


def _get_api_key(provider: str) -> str | None:
    if provider == PROVIDER_ANTHROPIC:
        return os.environ.get("MAGNOLIA_LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if provider == PROVIDER_DEEPSEEK:
        return os.environ.get("DEEPSEEK_API_KEY")
    if provider == PROVIDER_OPENAI:
        return os.environ.get("OPENAI_API_KEY")
    return None


def _get_model(provider: str) -> str:
    return os.environ.get("MAGNOLIA_LLM_MODEL") or _DEFAULT_MODEL[provider]


def _get_base_url(provider: str) -> str:
    env_name = _BASE_URL_ENV.get(provider)
    if env_name and (raw := os.environ.get(env_name)):
        base = raw.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        return base
    return _DEFAULT_BASE[provider]


def is_llm_available() -> bool:
    p = _resolve_provider()
    return bool(p and _get_api_key(p))


def call_llm(system_prompt: str, user_content: str, max_tokens: int = 2000) -> str | None:
    """Call the resolved LLM provider. Returns text on success or None on
    any failure (no provider configured, missing key, network error,
    malformed response). NEVER raises."""
    provider = _resolve_provider()
    if not provider:
        return None
    key = _get_api_key(provider)
    if not key:
        return None
    model = _get_model(provider)
    try:
        if provider == PROVIDER_ANTHROPIC:
            return _call_anthropic(key, model, system_prompt, user_content, max_tokens)
        return _call_openai_compat(provider, key, model, system_prompt, user_content, max_tokens)
    except Exception:
        return None


def _call_anthropic(
    key: str, model: str, system_prompt: str, user_content: str, max_tokens: int
) -> str | None:
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    if not resp.content:
        return None
    return resp.content[0].text


def _call_openai_compat(
    provider: str, key: str, model: str, system_prompt: str, user_content: str, max_tokens: int
) -> str | None:
    """DeepSeek + OpenAI both use the OpenAI chat completions schema."""
    url = f"{_get_base_url(provider)}/chat/completions"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    return msg.get("content")


def call_llm_json(system_prompt: str, user_content: str, max_tokens: int = 2000) -> dict | list | None:
    """Call LLM and parse JSON from the response. Strips a single
    ```json fenced block if present. Returns None on parse failure."""
    text = call_llm(system_prompt, user_content, max_tokens)
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None
