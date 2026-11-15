# OpenRouter prompt-caching information flow

## Routing metadata

1. `resolve_route(role)` labels critic and memory routes in `llm.Route.role`; `call_llm` is always a memory call.
2. `_openrouter_session_id` resolves `MAGNOLIA_PROJECT_DIR` (or the current directory), hashes the resolved
   path with SHA-256, and emits only `magnolia-<role>-<first 16 hex>`.
3. `_route_request` and `_call_openai_compat` add that value to OpenRouter request bodies unless
   `MAGNOLIA_OPENROUTER_STICKY` is `0`, `false`, `no` or `off`.
4. `_openrouter_provider` supplies the default quantization filter. A valid member value in
   `MAGNOLIA_OPENROUTER_PROVIDER` replaces it; a no-endpoint response makes `_post_openrouter` retry once
   without the provider field and surfaces `quality_floor_dropped` to timing and critic rows.

## Reused extractor prefix

1. `AutomaticMemoryExtractor.distill_transcript` reads `MAGNOLIA_DISTILL_SECOND_PASS` once.
2. When enabled, it passes the original transcript as `cache_prefix` on both extraction calls; when disabled,
   it passes `None`.
3. `call_llm_json` forwards the value to `call_llm`, then `_user_content` checks both that the user content
   starts with the prefix and that the target is native Anthropic or OpenRouter `anthropic/*`.
4. Matching targets receive a first text block containing the transcript and
   `cache_control: {"type": "ephemeral"}`, followed by an unmarked remainder block when non-empty.
   Kimi and providers with automatic caching retain the original plain string.

No critic, handover merge, consolidation or promotion caller supplies `cache_prefix`.
