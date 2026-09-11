"""LLM integration: provider-aware (DeepSeek / Anthropic / OpenAI / Kimi).

Magnolia uses the LLM for memory extraction, retrieval re-ranking,
compaction, and (via the magnolia wrapper) GOAL.md scaffolding. Until
2026-05-30 this module was hardcoded to Anthropic, which silently
failed when the operator only had DeepSeek / OpenAI keys configured —
the heuristic fallback then flooded staging with content-free entries
(see commit cab67c0). This rewrite makes the provider negotiable.

## Resolution order

The single user-facing knob is `MAGNOLIA_LLM_MODEL` — the provider is
*derived from the model name* (step 2) and normally never configured.

1. `MAGNOLIA_LLM_PROVIDER` (`deepseek`/`anthropic`/`openai`/`kimi`) —
   explicit override, for proxy/OpenAI-compatible endpoints where the model
   name does not identify the vendor. Not part of normal setup.
2. Model-name prefix: `deepseek-*` → deepseek, `claude*` → anthropic,
   `gpt-*`/`o1-*`/`o3-*`/`o4-*`/`chatgpt*` → openai, `kimi*`/`k3*`/
   `moonshot*` → kimi.
3. Autodetect by first key present, in order: `MAGNOLIA_LLM_API_KEY`
   (backward-compat: this was the old hardcoded Anthropic path) →
   `DEEPSEEK_API_KEY` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → Kimi
   (`KIMI_PLAN_MAGNOLIA_API_KEY` / `KIMI_API_KEY`). DeepSeek-first is
   intentional: it's the cheapest and the most commonly-configured key in
   the Magnolia user environment. Kimi is last so adding a Kimi key never
   hijacks an existing setup — select it explicitly via `MAGNOLIA_LLM_PROVIDER=kimi`
   or a `kimi-*` model name.
4. No `MAGNOLIA_LLM_MODEL` → the per-provider default model.

## Configuration

- `MAGNOLIA_MEMORY_MODEL` — the only user-facing knob: which model does the
  memory/background work. (The old name `MAGNOLIA_LLM_MODEL` still works but
  warns once — it read as "the LLM model", ambiguous with the main agent
  model configured in opencode.)
- `MAGNOLIA_MEMORY_PROVIDER` — explicit provider override for proxy /
  OpenAI-compatible endpoints where the model name doesn't identify the
  vendor. (Legacy `MAGNOLIA_LLM_PROVIDER` still honored, warns once.)
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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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


def _explicit_provider() -> str | None:
    """A valid MAGNOLIA_MEMORY_PROVIDER, else None (invalid values fall
    through). Legacy MAGNOLIA_LLM_PROVIDER still honored with a warning."""
    raw = (os.environ.get("MAGNOLIA_MEMORY_PROVIDER") or "").strip().lower()
    if not raw:
        legacy = (os.environ.get("MAGNOLIA_LLM_PROVIDER") or "").strip()
        if legacy:
            print("[llm] MAGNOLIA_LLM_PROVIDER is deprecated; "
                  "rename it to MAGNOLIA_MEMORY_PROVIDER", file=sys.stderr)
            raw = legacy.lower()
    if raw in _VALID_PROVIDERS:
        return raw
    return None


def _autodetect_provider() -> str | None:
    """Provider from whichever API key is present (back-compat order)."""
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


def _resolve_provider() -> str | None:
    """Pick a provider ignoring any model-name signal (back-compat helper)."""
    return _explicit_provider() or _autodetect_provider()


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


# Unrendered-template and stub values. A past opencode.json shipped with
# "@@DISTILL_MODEL@@" left unrendered (2026-08-27 diagnosis): _get_model then
# returned the literal string, every LLM call 400'd, and handover/distill/
# re-ranking silently degraded to heuristics for weeks. Guard: treat such
# values as unset and fall back to the provider default.
_PLACEHOLDER_RE = re.compile(r"@@|\{\{|PLACEHOLDER|TBD_|_TBD|YOUR_|DISTILL_MODEL")

# Model-name prefix → provider. The model name is the single setup knob;
# the vendor follows from it (deepseek-* is obviously DeepSeek's, etc.).
_MODEL_PREFIX_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("deepseek", PROVIDER_DEEPSEEK),
    ("claude", PROVIDER_ANTHROPIC),
    ("gpt-", PROVIDER_OPENAI),
    ("o1", PROVIDER_OPENAI),
    ("o3", PROVIDER_OPENAI),
    ("o4", PROVIDER_OPENAI),
    ("chatgpt", PROVIDER_OPENAI),
    ("kimi", PROVIDER_KIMI),
    ("k3", PROVIDER_KIMI),
    ("moonshot", PROVIDER_KIMI),
)


def _provider_for_model(model: str) -> str | None:
    m = model.strip().lower()
    for prefix, provider in _MODEL_PREFIX_PROVIDERS:
        if m.startswith(prefix):
            return provider
    return None


def _requested_model() -> str | None:
    """The memory model from the environment, placeholder-guard applied.

    MAGNOLIA_MEMORY_MODEL is the name; legacy MAGNOLIA_LLM_MODEL still works
    but warns once. Returns None when unset or placeholder-looking (caller
    then uses the provider default) — an unrendered '@@...@@' must never
    reach an API call (it 400s every call and silently degrades memory work;
    2026-08 freeze)."""
    raw = (os.environ.get("MAGNOLIA_MEMORY_MODEL") or "").strip()
    if not raw:
        legacy = (os.environ.get("MAGNOLIA_LLM_MODEL") or "").strip()
        if legacy:
            print("[llm] MAGNOLIA_LLM_MODEL is deprecated; "
                  "rename it to MAGNOLIA_MEMORY_MODEL", file=sys.stderr)
            raw = legacy
    if raw and _PLACEHOLDER_RE.search(raw):
        print(f"[llm] memory model {raw!r} looks like an unrendered "
              f"placeholder; ignoring it", file=sys.stderr)
        return None
    return raw or None


def _resolve_call() -> tuple[str | None, str | None]:
    """Full (provider, model) resolution per the documented order:
    explicit provider → model-name prefix → key autodetect; default model
    when none requested. (None, None) when nothing is configured."""
    model = _requested_model()
    provider = (
        _explicit_provider()
        or (model and _provider_for_model(model))
        or _autodetect_provider()
    )
    if not provider:
        return None, None
    return provider, (model or _DEFAULT_MODEL[provider])


def _get_model(provider: str) -> str:
    return _requested_model() or _DEFAULT_MODEL[provider]


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


def _record_timing(provider: str | None, model: str | None, ms: float,
                   outcome: str, **extra) -> None:
    """Append one telemetry row per call_llm attempt. Best-effort: telemetry
    must never break (or slow down) the call it measures. Location mirrors the
    project-pinned convention: <MAGNOLIA_PROJECT_DIR>/.magnolia/llm-timing.jsonl."""
    try:
        base = Path(os.environ.get("MAGNOLIA_PROJECT_DIR", ".")) / ".magnolia"
        base.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "ms": round(ms),
            "outcome": outcome,   # ok | empty | no_provider | no_key | error
            **extra,
        }
        with open(base / "llm-timing.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        pass


def call_llm(
    system_prompt: str,
    user_content: str,
    max_tokens: int = 2000,
    *,
    temperature: float | None = None,
    disable_thinking: bool = False,
    return_finish_reason: bool = False,
) -> str | None | tuple[str | None, str | None]:
    """Call the resolved LLM provider. Returns text on success or None on
    any failure (no provider configured, missing key, network error,
    malformed response). NEVER raises.

    With ``return_finish_reason=True`` returns ``(text, finish_reason)`` so a
    caller (e.g. the handover merge) can tell a complete answer from one cut
    off at ``max_tokens`` (``finish_reason == "length"``). On any failure the
    tuple is ``(None, None)``.

    `temperature` (when set) and `disable_thinking` (DeepSeek reasoning models —
    sends `thinking: {"type": "disabled"}`) make a call deterministic and stop a
    reasoning model from spending its output budget on reasoning_content. Kimi
    always sends thinking-disabled (see _call_kimi), so the flag is a no-op there.

    Every attempt appends a timing row (see _record_timing) so slow or silently
    failing calls are decomposable after the fact instead of invisible."""
    provider, model = _resolve_call()
    if not provider:
        _record_timing(None, None, 0, "no_provider")
        return None
    key = _get_api_key(provider)
    if not key:
        _record_timing(provider, None, 0, "no_key")
        return None
    t0 = time.monotonic()
    try:
        if provider == PROVIDER_ANTHROPIC:
            out, finish = _call_anthropic(key, model, system_prompt, user_content, max_tokens,
                                          temperature)
        elif provider == PROVIDER_KIMI:
            out, finish = _call_kimi(key, model, system_prompt, user_content, max_tokens,
                                     temperature)
        else:
            out, finish = _call_openai_compat(provider, key, model, system_prompt, user_content,
                                              max_tokens, temperature, disable_thinking)
        _record_timing(provider, model, (time.monotonic() - t0) * 1000,
                       "ok" if out else "empty", chars=len(out) if out else 0)
        return (out, finish) if return_finish_reason else out
    except Exception as e:  # noqa: BLE001 - contract: never raise
        _record_timing(provider, model, (time.monotonic() - t0) * 1000, "error",
                       error=type(e).__name__)
        return (None, None) if return_finish_reason else None


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
        return None, resp.stop_reason
    return resp.content[0].text, resp.stop_reason


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
            return block.get("text"), data.get("stop_reason")
    return None, data.get("stop_reason")


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
        return None, None
    choice = choices[0]
    msg = choice.get("message") or {}
    return msg.get("content"), choice.get("finish_reason")


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
