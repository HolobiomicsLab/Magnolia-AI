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
        "MAGNOLIA_OPENROUTER_PROVIDER", "MAGNOLIA_OPENROUTER_STICKY",
        "MAGNOLIA_DIRECT_FALLBACK_PROVIDER", "MAGNOLIA_DIRECT_FALLBACK_MODEL",
        "MAGNOLIA_PROJECT_DIR", "MAGNOLIA_PROJECT_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)
    llm._INVALID_OPENROUTER_PROVIDER_WARNED.clear()
    llm._OPENROUTER_FAILURE_WARNED.clear()


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
    assert captured["json"]["provider"] == {
        "quantizations": ["fp8", "bf16", "fp16", "fp32", "unknown"],
    }
    assert captured["json"]["session_id"] == llm._openrouter_session_id("memory")


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
        "provider": provider,
        "session_id": llm._openrouter_session_id("memory"),
        "reasoning": {"enabled": False},
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


def test_invalid_provider_json_uses_default_floor_with_one_stderr_line(monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_OPENROUTER_PROVIDER", "not-json")
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key", "openai",
    )

    first = llm._route_request(route, "s", "u", 1, None, False, False)[1]
    second = llm._route_request(route, "s", "u", 1, None, False, False)[1]

    expected = {"quantizations": ["fp8", "bf16", "fp16", "fp32", "unknown"]}
    assert first["provider"] == expected
    assert second["provider"] == expected
    assert capsys.readouterr().err.count("MAGNOLIA_OPENROUTER_PROVIDER") == 1


def test_openrouter_session_id_is_stable_per_project_and_role(monkeypatch, tmp_path):
    _clear_provider_env(monkeypatch)
    first_project = tmp_path / "LFX private project"
    second_project = tmp_path / "other project"
    first_project.mkdir()
    second_project.mkdir()
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(first_project))
    memory = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key",
        "openai", "memory",
    )
    critic = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key",
        "openai", "critic",
    )

    memory_id = llm._route_request(memory, "s", "u", 1, None, False, False)[1]["session_id"]
    repeated = llm._route_request(memory, "s", "u", 1, None, False, False)[1]["session_id"]
    critic_id = llm._route_request(critic, "s", "u", 1, None, False, False)[1]["session_id"]
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(second_project))
    other_id = llm._route_request(memory, "s", "u", 1, None, False, False)[1]["session_id"]

    assert memory_id == repeated
    assert memory_id.startswith("magnolia-memory-")
    assert critic_id.startswith("magnolia-critic-")
    assert len(memory_id.rsplit("-", 1)[1]) == 16
    assert len({memory_id, critic_id, other_id}) == 3
    assert str(first_project) not in memory_id
    assert first_project.name not in memory_id


def test_openrouter_session_id_reuses_project_resolver(monkeypatch, tmp_path):
    _clear_provider_env(monkeypatch)
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "active"
    marker_dir = workspace / ".magnolia"
    project.mkdir(parents=True)
    marker_dir.mkdir()
    (marker_dir / ".active-project").write_text("active")
    monkeypatch.chdir(workspace)

    active_id = llm._openrouter_session_id("memory")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", "projects/active")
    relative_id = llm._openrouter_session_id("memory")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", "")
    empty_id = llm._openrouter_session_id("memory")

    assert active_id == relative_id == empty_id


def test_openrouter_session_id_normalises_symlinked_project(monkeypatch, tmp_path):
    _clear_provider_env(monkeypatch)
    project = tmp_path / "project"
    alias = tmp_path / "alias"
    project.mkdir()
    alias.symlink_to(project, target_is_directory=True)

    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(project))
    direct_id = llm._openrouter_session_id("memory")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(alias))

    assert llm._openrouter_session_id("memory") == direct_id


def test_openrouter_session_id_honours_root_mode_marker(monkeypatch, tmp_path):
    _clear_provider_env(monkeypatch)
    project = tmp_path / "project"
    other = tmp_path / "other"
    (project / ".magnolia").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("goal")
    other.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(other))

    marked_id = llm._openrouter_session_id("memory")
    (project / ".magnolia" / "GOAL.md").unlink()
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(project))

    assert llm._openrouter_session_id("memory") == marked_id


@pytest.mark.parametrize("disabled", ["0", "false", "NO", "off"])
def test_openrouter_session_id_can_be_disabled(monkeypatch, disabled):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_OPENROUTER_STICKY", disabled)
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key", "openai",
    )
    body = llm._route_request(route, "s", "u", 1, None, False, False)[1]
    assert "session_id" not in body


def test_non_openrouter_route_has_no_session_id(monkeypatch):
    _clear_provider_env(monkeypatch)
    route = llm.Route(
        llm.PROVIDER_OPENAI, "gpt-4o-mini", "https://openai/chat", "key", "openai",
    )
    body = llm._route_request(route, "s", "u", 1, None, False, False)[1]
    assert "session_id" not in body


def test_default_quality_floor_and_member_override(monkeypatch):
    _clear_provider_env(monkeypatch)
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key", "openai",
    )
    default_body = llm._route_request(route, "s", "u", 1, None, False, False)[1]
    assert default_body["provider"] == {
        "quantizations": ["fp8", "bf16", "fp16", "fp32", "unknown"],
    }

    member_value = {"order": ["DeepInfra"], "allow_fallbacks": True}
    monkeypatch.setenv("MAGNOLIA_OPENROUTER_PROVIDER", json.dumps(member_value))
    member_body = llm._route_request(route, "s", "u", 1, None, False, False)[1]
    assert member_body["provider"] == member_value


def test_reasoning_and_floor_retries_never_exceed_three_posts(monkeypatch):
    _clear_provider_env(monkeypatch)
    bodies = []

    class Response:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

        def json(self):
            return {
                "model": "vendor/model", "provider": "DeepInfra",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }

    responses = [
        Response(400, "reasoning is unsupported; no endpoints found"),
        Response(400, "No endpoints found"),
        Response(200),
    ]
    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: bodies.append(kwargs["json"].copy()) or responses.pop(0),
    )
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key",
        "openai", "critic",
    )
    result = llm.call_route(route, "s", "u", max_tokens=1, disable_thinking=True)

    assert result.text == "OK"
    assert result.quality_floor_dropped is True
    assert len(bodies) == 3
    assert "reasoning" in bodies[0] and "reasoning" not in bodies[1]
    assert "provider" in bodies[1] and "provider" not in bodies[2]


def test_member_provider_is_never_dropped(monkeypatch):
    _clear_provider_env(monkeypatch)
    member_value = {"order": ["DeepInfra"], "allow_fallbacks": False, "zdr": True}
    monkeypatch.setenv("MAGNOLIA_OPENROUTER_PROVIDER", json.dumps(member_value))
    bodies = []

    class Response:
        status_code = 404
        text = "No endpoints found"

        def json(self):
            return {}

    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: bodies.append(kwargs["json"].copy()) or Response(),
    )
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key", "openai",
    )

    result = llm.call_route(route, "s", "u", max_tokens=1)

    assert result.status == 404
    assert result.quality_floor_dropped is False
    assert len(bodies) == 1
    assert bodies[0]["provider"] == member_value


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (400, "This endpoint's maximum context length is 128000 tokens"),
        (400, "max tokens exceeds the endpoint limit"),
        (400, "endpoint rate limit exceeded"),
        (404, "Model not found"),
    ],
)
def test_unrelated_openrouter_errors_do_not_drop_floor(monkeypatch, status, text):
    _clear_provider_env(monkeypatch)
    bodies = []

    class Response:
        status_code = status

        def __init__(self):
            self.text = text

        def json(self):
            return {}

    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: bodies.append(kwargs["json"].copy()) or Response(),
    )
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key", "openai",
    )

    result = llm.call_route(route, "s", "u", max_tokens=1)

    assert result.status == status
    assert result.quality_floor_dropped is False
    assert len(bodies) == 1
    assert bodies[0]["provider"] == llm._OPENROUTER_QUALITY_FLOOR


def test_call_llm_drops_quality_floor_on_endpoint_400_and_records_it(
    monkeypatch, tmp_path,
):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    bodies = []

    class Response:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError("fallback did not recover")

        def json(self):
            return {
                "model": "deepseek/deepseek-v4.1-flash", "provider": "DeepInfra",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }

    responses = [Response(400, "No endpoints match the quantization"), Response(200)]
    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: bodies.append(kwargs["json"].copy()) or responses.pop(0),
    )

    assert llm.call_llm("s", "u", max_tokens=1) == "OK"
    timing = json.loads(
        (tmp_path / ".magnolia" / "llm-timing.jsonl").read_text().splitlines()[-1]
    )
    assert len(bodies) == 2
    assert "provider" in bodies[0] and "provider" not in bodies[1]
    assert timing["quality_floor_dropped"] is True


def test_floor_drop_survives_transport_error_for_route(monkeypatch):
    _clear_provider_env(monkeypatch)
    calls = 0

    class Response:
        status_code = 404
        text = "No endpoints found"

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response()
        raise RuntimeError("transport failed")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "vendor/model", "https://router/chat", "key", "openai",
    )

    result = llm.call_route(route, "s", "u", max_tokens=1)

    assert calls == 2
    assert result.error == "transport failed"
    assert result.quality_floor_dropped is True


def test_floor_drop_survives_transport_error_in_timing(monkeypatch, tmp_path):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    calls = 0

    class Response:
        status_code = 404
        text = "No endpoints found"

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response()
        raise RuntimeError("transport failed")

    monkeypatch.setattr(llm.httpx, "post", fake_post)

    assert llm.call_llm("s", "u", max_tokens=1) is None
    timing = json.loads(
        (tmp_path / ".magnolia" / "llm-timing.jsonl").read_text().splitlines()[-1]
    )
    assert calls == 2
    assert timing["outcome"] == "error"
    assert timing["quality_floor_dropped"] is True


@pytest.mark.parametrize("model", ["deepseek/deepseek-v4.1-flash", "openai/gpt-5"])
def test_automatic_cache_models_keep_plain_user_string(monkeypatch, model):
    _clear_provider_env(monkeypatch)
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, model, "https://router/chat", "key", "openai",
    )
    body = llm._route_request(
        route, "s", "transcript plus instruction", 1, None, False, False,
        cache_prefix="transcript",
    )[1]
    assert body["messages"][1]["content"] == "transcript plus instruction"


def test_openrouter_anthropic_cache_prefix_marks_only_first_block(monkeypatch):
    _clear_provider_env(monkeypatch)
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "anthropic/claude-sonnet-4.5",
        "https://router/chat", "key", "openai",
    )
    body = llm._route_request(
        route, "s", "transcript plus instruction", 1, None, False, False,
        cache_prefix="transcript",
    )[1]
    assert body["messages"][1]["content"] == [
        {
            "type": "text", "text": "transcript",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": " plus instruction"},
    ]


def test_cache_prefix_is_ignored_when_user_does_not_start_with_it(monkeypatch):
    _clear_provider_env(monkeypatch)
    route = llm.Route(
        llm.PROVIDER_OPENROUTER, "anthropic/claude-sonnet-4.5",
        "https://router/chat", "key", "openai",
    )
    body = llm._route_request(
        route, "s", "different text", 1, None, False, False,
        cache_prefix="transcript",
    )[1]

    assert body["messages"][1]["content"] == "different text"


def test_native_anthropic_route_marks_prefix_but_kimi_does_not(monkeypatch):
    _clear_provider_env(monkeypatch)
    native = llm.Route(
        llm.PROVIDER_ANTHROPIC, "claude-haiku-4-5", "https://anthropic/messages",
        "key", "anthropic",
    )
    kimi = llm.Route(
        llm.PROVIDER_KIMI, "k3", "https://kimi/messages", "key", "anthropic",
    )
    native_body = llm._route_request(
        native, "s", "transcript", 1, None, False, False, cache_prefix="transcript",
    )[1]
    kimi_body = llm._route_request(
        kimi, "s", "transcript", 1, None, False, False, cache_prefix="transcript",
    )[1]

    assert native_body["messages"][0]["content"] == [{
        "type": "text", "text": "transcript",
        "cache_control": {"type": "ephemeral"},
    }]
    assert kimi_body["messages"][0]["content"] == "transcript"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_explicit_openrouter_failure_never_autodetects_direct_key(
    monkeypatch, capsys, status,
):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-key-must-not-be-used")
    calls = []

    class Response:
        status_code = status
        text = "upstream unavailable"

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(
        llm.httpx, "post", lambda url, **kwargs: calls.append(url) or Response(),
    )
    route = llm.Route("openrouter", "deepseek/model", "https://router/chat", "or-key", "openai")

    first = llm.call_route(route, "s", "u", max_tokens=1)
    second = llm.call_route(route, "s", "u", max_tokens=1)

    assert first.status == status and first.error.startswith(f"HTTP {status}")
    assert len(calls) == 2 and all("router" in call for call in calls)
    assert capsys.readouterr().err.count("OpenRouter memory route failed") == 1


def test_explicit_openrouter_missing_key_is_typed_and_visible(
    tmp_path, monkeypatch, capsys,
):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    magnolia = tmp_path / ".magnolia"
    magnolia.mkdir()
    (magnolia / ".current-session-id").write_text("session-one")
    route = llm.Route("openrouter", "deepseek/model", "https://router/chat", "", "openai")

    result = llm.call_route(route, "s", "u", max_tokens=1)
    llm.call_route(route, "s", "u", max_tokens=1)

    assert result.error == "missing API key for openrouter" and result.status is None
    assert "OpenRouter memory route failed" in capsys.readouterr().err
    assert len((magnolia / ".distill-notices").read_text().splitlines()) == 1


def test_configured_direct_fallback_is_the_only_alternate_route(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_DIRECT_FALLBACK_PROVIDER", "deepseek")
    monkeypatch.setenv("MAGNOLIA_DIRECT_FALLBACK_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-key")
    calls = []

    class Response:
        text = ""

        def __init__(self, status, data):
            self.status_code = status
            self._data = data

        def json(self):
            return self._data

    replies = [
        Response(503, {}),
        Response(200, {"model": "deepseek-flash", "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}]}),
    ]
    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: calls.append((url, kwargs["headers"]["Authorization"])) or replies.pop(0),
    )
    route = llm.Route("openrouter", "deepseek/model", "https://router/chat", "or-key", "openai")

    result = llm.call_route(route, "s", "u", max_tokens=1)

    assert result.text == "recovered"
    assert result.fallback_provider == "deepseek" and result.fallback_model == "deepseek-flash"
    assert calls == [
        ("https://router/chat", "Bearer or-key"),
        ("https://api.deepseek.com/v1/chat/completions", "Bearer direct-key"),
    ]


def test_call_llm_empty_openrouter_reply_is_visible_failure(
    tmp_path, monkeypatch, capsys,
):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "deepseek/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-key-must-not-be-used")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    magnolia = tmp_path / ".magnolia"
    magnolia.mkdir()
    (magnolia / ".current-session-id").write_text("session-one")
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"model": "deepseek/model", "choices": []}

    monkeypatch.setattr(
        llm.httpx, "post", lambda url, **kwargs: calls.append(url) or Response(),
    )

    assert llm.call_llm("s", "u", max_tokens=1) is None
    assert llm.call_llm("s", "u", max_tokens=1) is None
    timing = json.loads(
        (tmp_path / ".magnolia/llm-timing.jsonl").read_text().splitlines()[-1]
    )
    assert calls == [
        "https://openrouter.ai/api/v1/chat/completions",
        "https://openrouter.ai/api/v1/chat/completions",
    ]
    assert timing["outcome"] == "error" and timing["error"] == "empty response"
    assert timing["status"] == 200
    assert capsys.readouterr().err.count("OpenRouter memory route failed") == 1
    assert len((magnolia / ".distill-notices").read_text().splitlines()) == 1


def test_call_llm_missing_openrouter_key_is_visible_without_direct_autodetect(
    tmp_path, monkeypatch, capsys,
):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-key-must-not-be-used")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(
        llm.httpx, "post", lambda *args, **kwargs: pytest.fail("HTTP must not run"),
    )

    assert llm.call_llm("s", "u", max_tokens=1) is None
    timing = json.loads(
        (tmp_path / ".magnolia/llm-timing.jsonl").read_text().splitlines()[-1]
    )
    assert timing["outcome"] == "no_key" and timing["provider"] == "openrouter"
    assert "OpenRouter memory route failed" in capsys.readouterr().err


@pytest.mark.parametrize("status", [429, 503])
def test_call_llm_openrouter_http_failure_never_uses_unconfigured_fallback(
    tmp_path, monkeypatch, capsys, status,
):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-key-must-not-be-used")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    calls = []

    class Response:
        status_code = status
        text = "upstream unavailable"

        def raise_for_status(self):
            error = RuntimeError(f"HTTP {status}")
            error.response = self
            raise error

    monkeypatch.setattr(
        llm.httpx, "post", lambda url, **kwargs: calls.append(url) or Response(),
    )

    assert llm.call_llm("s", "u", max_tokens=1) is None
    timing = json.loads(
        (tmp_path / ".magnolia/llm-timing.jsonl").read_text().splitlines()[-1]
    )
    assert calls == ["https://openrouter.ai/api/v1/chat/completions"]
    assert timing["outcome"] == "error" and timing["status"] == status
    assert "OpenRouter memory route failed" in capsys.readouterr().err


def test_call_llm_uses_configured_direct_fallback_for_empty_reply(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("MAGNOLIA_DIRECT_FALLBACK_PROVIDER", "deepseek")
    monkeypatch.setenv("MAGNOLIA_DIRECT_FALLBACK_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "direct-key")
    calls = []

    class Response:
        def __init__(self, content):
            self.status_code = 200
            self._content = content

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "model", "choices": [{
                    "message": {"content": self._content}, "finish_reason": "stop",
                }],
            }

    replies = [Response(""), Response("recovered")]
    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: calls.append((url, kwargs["headers"]["Authorization"]))
        or replies.pop(0),
    )

    assert llm.call_llm("s", "u", max_tokens=1) == "recovered"
    assert calls == [
        ("https://openrouter.ai/api/v1/chat/completions", "Bearer or-key"),
        ("https://api.deepseek.com/v1/chat/completions", "Bearer direct-key"),
    ]
