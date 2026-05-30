"""Test-suite-wide fixtures.

`_isolate_llm_env` autouse: clears every env var the LLM resolver looks at
before each test. This prevents the test runner's shell environment
(e.g. DEEPSEEK_API_KEY exported in dev sessions) from accidentally
activating live LLM calls inside tests that aren't designed for it.

Tests that need to exercise the LLM path explicitly opt in via
`monkeypatch.setenv(...)` per-test (see tests/test_llm_providers.py).
"""
import pytest


_LLM_ENV_VARS = (
    "MAGNOLIA_LLM_PROVIDER",
    "MAGNOLIA_LLM_API_KEY",
    "MAGNOLIA_LLM_MODEL",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Clear LLM-related env vars before every test."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
