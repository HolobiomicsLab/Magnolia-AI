"""LLM integration: provider-aware (DeepSeek / Anthropic / OpenAI / Kimi).

Magnolia uses the LLM for memory extraction, retrieval re-ranking,
compaction, and (via the magnolia wrapper) GOAL.md scaffolding. Until
2026-05-30 this module was hardcoded to Anthropic, which silently
failed when the operator only had DeepSeek / OpenAI keys configured —
the heuristic fallback then flooded staging with content-free entries
(see commit cab67c0). This rewrite makes the provider negotiable.

## Resolution order

1. `MAGNOLIA_LLM_PROVIDER` (`deepseek`/`anthropic`/`openai`/`kimi`) — explicit override.
2. `MAGNOLIA_LLM_API_KEY` set → `anthropic` (backward-compat: this was the
   old hardcoded path, and an operator who deliberately set it likely meant
   "use this Anthropic key").
3. Autodetect by first key present, in order: `DEEPSEEK_API_KEY` →
   `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → Kimi (`KIMI_PLAN_MAGNOLIA_API_KEY`
   / `KIMI_API_KEY`). DeepSeek-first is intentional: it's the cheapest and
   the most commonly-configured key in the Magnolia user environment. Kimi
   is last so adding a Kimi key never hijacks an existing setup — select it
   explicitly via `MAGNOLIA_LLM_PROVIDER=kimi`.

## Configuration

- `MAGNOLIA_LLM_MODEL` overrides the per-provider default model.
- `DEEPSEEK_BASE_URL` / `OPENAI_BASE_URL` / `KIMI_BASE_URL` override the API
  base URL; `/v1` is appended automatically if missing (DeepSeek's docs show
  both forms).
- Kimi is the Kimi-for-Coding plan endpoint (`https://api.kimi.com/coding/v1`),
  which speaks the Anthropic Messages schema — key from
  `KIMI_PLAN_MAGNOLIA_API_KEY` (Magnolia-specific) or `KIMI_API_KEY`.
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
PROVIDER_KIMI = "kimi"

_VALID_PROVIDERS = {PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC, PROVIDER_OPENAI, PROVIDER_KIMI}

_DEFAULT_MODEL = {
    # deepseek-chat is a back-compat alias for deepseek-v4-flash, deprecated
    # 2026-07-24; use the explicit v4 id (1M context, cheap). Override per
    # provider via MAGNOLIA_LLM_MODEL (e.g. deepseek-v4-pro for higher recall).
    PROVIDER_DEEPSEEK: "deepseek-v4-flash",
    PROVIDER_ANTHROPIC: "claude-haiku-4-5-20251001",
    PROVIDER_OPENAI: "gpt-4o-mini",
    # Kimi K3 on the Kimi-for-Coding plan endpoint (models.dev id "k3").
    PROVIDER_KIMI: "k3",
}

_DEFAULT_BASE = {
    PROVIDER_DEEPSEEK: "https://api.deepseek.com/v1",
    PROVIDER_OPENAI: "https://api.openai.com/v1",
    PROVIDER_KIMI: "https://api.kimi.com/coding/v1",
}

_BASE_URL_ENV = {
    PROVIDER_DEEPSEEK: "DEEPSEEK_BASE_URL",
    PROVIDER_OPENAI: "OPENAI_BASE_URL",
    PROVIDER_KIMI: "KIMI_BASE_URL",
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
    if os.environ.get("KIMI_PLAN_MAGNOLIA_API_KEY") or os.environ.get("KIMI_API_KEY"):
        return PROVIDER_KIMI
    return None


def _get_api_key(provider: str) -> str | None:
    if provider == PROVIDER_ANTHROPIC:
        return os.environ.get("MAGNOLIA_LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if provider == PROVIDER_DEEPSEEK:
        return os.environ.get("DEEPSEEK_API_KEY")
    if provider == PROVIDER_OPENAI:
        return os.environ.get("OPENAI_API_KEY")
    if provider == PROVIDER_KIMI:
        # Magnolia-specific plan key first; KIMI_API_KEY is the models.dev convention.
        return os.environ.get("KIMI_PLAN_MAGNOLIA_API_KEY") or os.environ.get("KIMI_API_KEY")
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


def call_llm(
    system_prompt: str,
    user_content: str,
    max_tokens: int = 2000,
    *,
    temperature: float | None = None,
    disable_thinking: bool = False,
) -> str | None:
    """Call the resolved LLM provider. Returns text on success or None on
    any failure (no provider configured, missing key, network error,
    malformed response). NEVER raises.

    `temperature` (when set) and `disable_thinking` (DeepSeek reasoning models —
    sends `thinking: {"type": "disabled"}`) make a call deterministic and stop a
    reasoning model from spending its output budget on reasoning_content. Kimi
    always sends thinking-disabled (see _call_kimi), so the flag is a no-op there."""
    provider = _resolve_provider()
    if not provider:
        return None
    key = _get_api_key(provider)
    if not key:
        return None
    model = _get_model(provider)
    try:
        if provider == PROVIDER_ANTHROPIC:
            return _call_anthropic(key, model, system_prompt, user_content, max_tokens,
                                   temperature)
        if provider == PROVIDER_KIMI:
            return _call_kimi(key, model, system_prompt, user_content, max_tokens,
                              temperature)
        return _call_openai_compat(provider, key, model, system_prompt, user_content,
                                   max_tokens, temperature, disable_thinking)
    except Exception:
        return None


def _call_anthropic(
    key: str, model: str, system_prompt: str, user_content: str, max_tokens: int,
    temperature: float | None = None,
) -> str | None:
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client.messages.create(**kwargs)
    if not resp.content:
        return None
    return resp.content[0].text


def _call_kimi(
    key: str, model: str, system_prompt: str, user_content: str, max_tokens: int,
    temperature: float | None = None,
) -> str | None:
    """Kimi-for-Coding speaks the Anthropic Messages schema (models.dev lists its
    sdk as @ai-sdk/anthropic). Plain httpx, mirroring _call_openai_compat, so no
    Anthropic-SDK base_url semantics sneak in. Sends both auth header styles —
    the endpoint accepts x-api-key (ai-sdk) or Bearer (Claude Code integrations).

    Thinking is always disabled: K3 thinks by default and callers are all memory
    busywork (extraction/classification/summaries) with max_tokens as small as
    300 — default thinking can eat the whole budget and return no text block
    (call_llm then surfaces None and callers fall back to heuristics)."""
    url = f"{_get_base_url(PROVIDER_KIMI)}/messages"
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "thinking": {"type": "disabled"},
    }
    if temperature is not None:
        body["temperature"] = temperature
    resp = httpx.post(
        url,
        headers={
            "x-api-key": key,
            "Authorization": f"Bearer {key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    # Thinking models may lead with a non-text block; take the first text block.
    for block in data.get("content") or []:
        if block.get("type") == "text":
            return block.get("text")
    return None


def _call_openai_compat(
    provider: str, key: str, model: str, system_prompt: str, user_content: str, max_tokens: int,
    temperature: float | None = None, disable_thinking: bool = False,
) -> str | None:
    """DeepSeek + OpenAI both use the OpenAI chat completions schema."""
    url = f"{_get_base_url(provider)}/chat/completions"
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    if temperature is not None:
        body["temperature"] = temperature
    # DeepSeek reasoning models (v4-flash/pro) put output in reasoning_content and
    # exhaust max_tokens on it; disabling thinking gives a direct, cheaper answer.
    if disable_thinking and provider == PROVIDER_DEEPSEEK:
        body["thinking"] = {"type": "disabled"}
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    return msg.get("content")


def call_llm_json(
    system_prompt: str,
    user_content: str,
    max_tokens: int = 2000,
    *,
    temperature: float | None = None,
    disable_thinking: bool = False,
) -> dict | list | None:
    """Call LLM and parse JSON from the response. Strips a single
    ```json fenced block if present. Returns None on parse failure."""
    text = call_llm(system_prompt, user_content, max_tokens,
                    temperature=temperature, disable_thinking=disable_thinking)
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None
