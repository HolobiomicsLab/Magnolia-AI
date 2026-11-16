"""Claim-critic routing, request parity, trace building, and persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from compchem_memory import llm
from compchem_memory.hook import critic
from compchem_memory.hook.action_retrieval import probe_application
from compchem_memory.hook.events import AgentRef, HookEvent, ToolCall
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState
from compchem_memory.hook.transcript import append_event, read_turns, transcript_path
from compchem_memory.llm import CallResult, Route


KEY_VARS = (
    "MAGNOLIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
    "ZHIPU_API_KEY", "ZAI_API_KEY", "DEEPSEEK_BASE_URL", "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL", "KIMI_BASE_URL", "OPENROUTER_BASE_URL", "MISTRAL_BASE_URL",
    "ZAI_BASE_URL", "MAGNOLIA_MEMORY_PROVIDER", "MAGNOLIA_MEMORY_MODEL",
    "MAGNOLIA_LLM_PROVIDER", "MAGNOLIA_LLM_MODEL", "MAGNOLIA_CRITIC_PROVIDER",
    "MAGNOLIA_CRITIC_MODEL", "MAGNOLIA_CRITIC_API_KEY", "MAGNOLIA_CRITIC_BASE_URL",
    "MAGNOLIA_OPENROUTER_PROVIDER",
    "MAGNOLIA_CRITIC", "MAGNOLIA_PROJECT_DIR", "MAGNOLIA_PROJECT_WORKSPACE",
)

@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch):
    """Remove every provider selector and key before each test."""
    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def _paths(tmp_path: Path) -> ProjectPaths:
    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    return ProjectPaths(tmp_path, project, store, "project", False)


def _event(kind: str, *, session: str = "s1", agent: AgentRef | None = None, **values) -> HookEvent:
    return HookEvent(
        harness="claude", event=kind, session_id=session, cwd="/project",
        turn_id=values.pop("turn_id", "t1"), agent=agent, **values,
    )


def _tool(
    name: str,
    call_id: str,
    *,
    output: str | None = None,
    tool_input: dict | None = None,
    is_error: bool = False,
) -> ToolCall:
    return ToolCall(
        name=name, server=None, raw_name=name, call_id=call_id,
        input=tool_input or {}, output=output, is_error=is_error,
    )


@pytest.mark.parametrize(
    ("provider", "key_var", "key", "model", "url", "schema"),
    [
        ("deepseek", "DEEPSEEK_API_KEY", "ds", "deepseek-flash", "https://api.deepseek.com/v1/chat/completions", "openai"),
        ("anthropic", "ANTHROPIC_API_KEY", "an", "claude-haiku-4-5-20251001", "https://api.anthropic.com/v1/messages", "anthropic"),
        ("openai", "OPENAI_API_KEY", "op", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions", "openai"),
        ("kimi", "KIMI_PLAN_MAGNOLIA_API_KEY", "km", "k3", "https://api.kimi.com/coding/v1/messages", "anthropic"),
        ("openrouter", "OPENROUTER_API_KEY", "or", "deepseek/deepseek-v4.1-flash", "https://openrouter.ai/api/v1/chat/completions", "openai"),
        ("mistral", "MISTRAL_API_KEY", "mi", "mistral-small-latest", "https://api.mistral.ai/v1/chat/completions", "openai"),
        ("zai", "ZHIPU_API_KEY", "za", "glm-5.3-flash", "https://api.z.ai/api/paas/v4/chat/completions", "openai"),
    ],
)
def test_resolve_route_critic_matches_plugin(monkeypatch, provider, key_var, key, model, url, schema):
    monkeypatch.setenv("MAGNOLIA_CRITIC_PROVIDER", provider)
    monkeypatch.setenv(key_var, key)
    assert llm.resolve_route("critic") == Route(
        provider, model, url, key, schema, "critic",
    )


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"DEEPSEEK_API_KEY": "ds"}, ("deepseek", "deepseek-flash", "ds")),
        ({"MISTRAL_API_KEY": "mi", "DEEPSEEK_API_KEY": "ds"}, ("deepseek", "deepseek-flash", "ds")),
        ({"MAGNOLIA_CRITIC_MODEL": "mistral-medium-latest", "MISTRAL_API_KEY": "mi", "DEEPSEEK_API_KEY": "ds"}, ("mistral", "mistral-medium-latest", "mi")),
        ({"MAGNOLIA_CRITIC_PROVIDER": "zai", "ZHIPU_API_KEY": "za", "ZAI_BASE_URL": "https://api.z.ai/api/coding/paas/v4"}, ("zai", "glm-5.3-flash", "za")),
        ({"MAGNOLIA_CRITIC_MODEL": "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY": "an"}, ("anthropic", "claude-haiku-4-5-20251001", "an")),
        ({"MAGNOLIA_CRITIC_PROVIDER": "openai", "MAGNOLIA_CRITIC_API_KEY": "own", "MAGNOLIA_CRITIC_BASE_URL": "https://llm.example.org/api/", "MAGNOLIA_CRITIC_MODEL": "local-judge"}, ("openai", "local-judge", "own")),
        ({}, None),
        ({"MISTRAL_API_KEY": "mi"}, ("mistral", "mistral-small-latest", "mi")),
    ],
)
def test_resolve_route_critic_eight_plugin_cases(monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    route = llm.resolve_route("critic")
    if expected is None:
        assert route is None
    else:
        assert (route.provider, route.model, route.key) == expected


class FakeResponse:
    """Small httpx response stand-in used by request-shape tests."""

    def __init__(self, data: dict, status: int = 200, text: str = "") -> None:
        self._data = data
        self.status_code = status
        self.text = text

    def json(self) -> dict:
        return self._data


def test_call_route_shapes(monkeypatch):
    calls = []
    replies = [
        FakeResponse({"model": "claude-resolved", "content": [{"type": "thinking"}, {"type": "text", "text": "anthropic"}], "stop_reason": "max_tokens"}),
        FakeResponse({"model": "gpt-resolved", "choices": [{"message": {"content": "openai"}, "finish_reason": "length"}]}),
        FakeResponse({"usage": {"prompt_tokens": 7}}, status=401, text="bad credential"),
        FakeResponse({"choices": []}),
    ]

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return replies.pop(0)

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    anthropic = Route("kimi", "k3", "https://kimi/messages", "km", "anthropic")
    openai = Route("openrouter", "vendor/model", "https://router/chat/completions", "or", "openai")

    first = llm.call_route(anthropic, "sys", "user", max_tokens=12, temperature=0, disable_thinking=True)
    second = llm.call_route(openai, "sys", "user", max_tokens=13, temperature=0, json_mode=True)
    unauthorized = llm.call_route(openai, "sys", "user", max_tokens=1)
    empty = llm.call_route(openai, "sys", "user", max_tokens=1)

    assert first == CallResult("anthropic", "max_tokens", "claude-resolved", None, 200)
    assert calls[0][1]["json"]["thinking"] == {"type": "disabled"}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer km"
    assert second == CallResult("openai", "length", "gpt-resolved", None, 200)
    assert second.finish_reason == "length"
    assert calls[1][1]["json"]["response_format"] == {"type": "json_object"}
    assert calls[1][1]["headers"]["HTTP-Referer"] == "https://magnolia.local"
    assert unauthorized.status == 401 and "401" in unauthorized.error and "bad credential" in unauthorized.error
    assert unauthorized.usage == {"prompt_tokens": 7}
    assert empty.text is None and empty.error is None and empty.status == 200
    assert all(call[1]["timeout"] == 60 for call in calls)


def test_call_route_never_raises(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    route = Route("openai", "m", "https://example/chat", "k", "openai")
    assert llm.call_route(route, "s", "u", max_tokens=1) == CallResult(None, None, None, "offline", None)


@pytest.mark.parametrize("failure_kind", ["http", "exception"])
def test_call_route_scrubs_key_from_result_and_critic_log(tmp_path, monkeypatch, failure_kind):
    fabricated_key = "sk-fabricated-critic-key-1234567890"
    route = Route("openai", "judge", "https://judge/chat", fabricated_key, "openai")

    def fake_post(*args, **kwargs):
        if failure_kind == "http":
            detail = f"invalid key {fabricated_key}; Authorization: Bearer {fabricated_key}"
            return FakeResponse({}, status=401, text=detail)
        raise OSError(f"transport rejected key {fabricated_key}")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    result = llm.call_route(route, "system", "user", max_tokens=1)
    assert fabricated_key not in (result.error or "")

    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    append_event(paths.store, _event("prompt", prompt="rank"))
    append_event(paths.store, _event("turn-end", assistant_text="x" * 200))
    critic.run_critic(paths, "claude", "s1", None)

    error_log = (paths.store / "claim-critic" / "errors.jsonl").read_text()
    assert fabricated_key not in error_log
    assert "[REDACTED]" in error_log


def test_memory_route_anthropic_explicit(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic-proxy.example/api")
    assert llm.resolve_route("memory") == Route(
        "anthropic", "claude-haiku-4-5-20251001",
        "https://anthropic-proxy.example/api/v1/messages", "an", "anthropic",
    )


def test_legacy_llm_helpers_unchanged(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an")
    assert llm._resolve_call() == ("anthropic", "claude-haiku-4-5-20251001")
    assert llm._get_api_key("anthropic") == "an"
    assert llm._get_base_url("deepseek") == "https://api.deepseek.com/v1"
    with pytest.raises(KeyError):
        llm._get_base_url("anthropic")


def test_critic_report_length_boundary(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "0")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    calls = []
    monkeypatch.setattr(critic, "call_route", lambda *args, **kwargs: calls.append(args) or CallResult("{}", None, None, None, 200))
    append_event(paths.store, _event("prompt", session="short", prompt="p"))
    append_event(paths.store, _event("turn-end", session="short", assistant_text="x" * 199))
    critic.run_critic(paths, "claude", "short", None)
    append_event(paths.store, _event("prompt", session="boundary", prompt="p"))
    append_event(paths.store, _event("turn-end", session="boundary", assistant_text="x" * 200))
    critic.run_critic(paths, "claude", "boundary", None)
    assert len(calls) == 1


def test_build_trace_caps(tmp_path):
    store = tmp_path / ".magnolia"
    append_event(store, _event("prompt", prompt="old"))
    append_event(store, _event("post-tool", tool=_tool("read", "old", output="o" * 3000)))
    append_event(store, _event("prompt", prompt="current", turn_id="t2"))
    append_event(store, _event("post-tool", turn_id="t2", tool=_tool("read", "current", output="c" * 3000, tool_input={"text": "i" * 5000})))
    append_event(store, _event("post-tool", turn_id="t2", tool=_tool("memory_search", "evidence", output="e" * 3000)))
    append_event(store, _event("turn-end", turn_id="t2", assistant_text="report"))

    rows = read_turns(transcript_path(store, "claude", "s1"))
    report, trace_text = critic.build_trace(rows, None)
    trace = json.loads(trace_text)
    by_tool = {item["call_id"]: item for item in trace}
    assert report == "report"
    assert len(by_tool["old"]["output"]) == 500 and by_tool["old"]["turn"] == "earlier"
    assert len(by_tool["current"]["output"]) == 2000 and "turn" not in by_tool["current"]
    assert len(by_tool["evidence"]["output"]) == 3000
    assert isinstance(by_tool["current"]["input"], str) and len(by_tool["current"]["input"]) == 4000


def test_build_trace_one_item_per_call(tmp_path):
    store = tmp_path / ".magnolia"
    append_event(store, _event("prompt", prompt="p"))
    append_event(store, _event("pre-tool", tool=_tool("read", "paired", tool_input={"path": "a"})))
    append_event(store, _event("post-tool", tool=_tool("read", "paired", output="done", tool_input={"path": "a"})))
    append_event(store, _event("pre-tool", tool=_tool("shell", "unfinished", tool_input={"cmd": "sleep"})))
    _, trace_text = critic.build_trace(read_turns(transcript_path(store, "claude", "s1")), None)
    trace = json.loads(trace_text)
    assert [item["call_id"] for item in trace] == ["paired", "unfinished"]
    assert trace[0]["status"] == "completed"
    assert trace[1]["status"] == "unfinished" and trace[1]["output"] == ""


def test_build_trace_parent_and_child_apart(tmp_path):
    store = tmp_path / ".magnolia"
    child = AgentRef("child-1", "Explore")
    append_event(store, _event("prompt", prompt="parent prompt"))
    append_event(store, _event("post-tool", tool=_tool("read", "parent", output="p")))
    append_event(store, _event("agent-start", agent=child))
    append_event(store, _event("post-tool", agent=child, tool=_tool("shell", "child", output="c")))
    append_event(store, _event("turn-end", agent=child, assistant_text="child report"))
    append_event(store, _event("turn-end", assistant_text="parent report"))
    rows = read_turns(transcript_path(store, "claude", "s1"))

    parent_report, parent_trace = critic.build_trace(rows, None)
    child_report, child_trace = critic.build_trace(rows, "child-1")
    assert parent_report == "parent report"
    parent_items = json.loads(parent_trace)
    assert [item["call_id"] for item in parent_items if item["call_id"]] == ["parent", "child"]
    assert parent_items[-1]["tool"] == "SUB-AGENT REPORT"
    assert parent_items[-1]["agent"] == "[child child-1]"
    assert child_report == "child report"
    assert [item["call_id"] for item in json.loads(child_trace)] == ["child"]


def test_vibe_parent_trace_includes_time_linked_child_tool_and_reply(tmp_path):
    store = tmp_path / ".magnolia"
    child = AgentRef("child-1", "worker")
    append_event(store, HookEvent(
        "vibe", "prompt", "s1", "/project", turn_id="T1", prompt="go",
    ))
    append_event(store, HookEvent(
        "vibe", "post-tool", "s1", "/project", agent=child,
        tool=ToolCall("run_shell", "compchem-tools", "mcp_compchem-tools.run_shell",
                      "child-call", {"cmd": "x"}, "energy -12.3"),
    ))
    append_event(store, HookEvent(
        "vibe", "turn-end", "s1", "/project", turn_id="child-turn", agent=child,
        assistant_text="The child measured energy -12.3",
    ))
    append_event(store, HookEvent(
        "vibe", "turn-end", "s1", "/project", turn_id="T1",
        assistant_text="The child found energy -12.3",
    ))

    report, trace_text, count = critic._trace(
        read_turns(transcript_path(store, "vibe", "s1")), None, "T1",
    )
    trace = json.loads(trace_text)

    assert report == "The child found energy -12.3"
    assert count == 2
    assert trace[0]["call_id"] == "child-call"
    assert trace[0]["agent"] == "[child child-1]"
    assert trace[0]["output"] == "energy -12.3"
    assert trace[1]["tool"] == "SUB-AGENT REPORT"
    assert trace[1]["agent"] == "[child child-1]"
    assert trace[1]["output"] == "The child measured energy -12.3"


def test_vibe_parent_trace_includes_child_reply_recovered_after_main_reply():
    rows = [
        {"kind": "prompt", "turn_id": "T1", "agent": None, "text": "delegate"},
        {"kind": "tool", "phase": "post", "turn_id": None, "agent": {"id": "c1"},
         "name": "run_shell", "call_id": "child-call", "input": {}, "output": "42"},
        {"kind": "turn-boundary", "turn_id": "C1", "agent": {"id": "c1"}},
        {"kind": "assistant", "turn_id": "T1", "agent": None,
         "text": "The child reported 42."},
        {"kind": "assistant", "turn_id": "C1", "agent": {"id": "c1"},
         "text": "run_shell returned 42."},
        {"kind": "session-end", "turn_id": None, "agent": None},
    ]

    report, trace_text, _ = critic._trace(rows, None, "T1", harness="vibe")
    trace = json.loads(trace_text)

    assert report == "The child reported 42."
    [child_reply] = [item for item in trace if item["tool"] == "SUB-AGENT REPORT"]
    assert child_reply["agent"] == "[child c1]"
    assert child_reply["output"] == "run_shell returned 42."
    assert "turn" not in child_reply


def test_vibe_late_child_reply_matches_boundary_in_parent_window():
    rows = [
        {"kind": "prompt", "turn_id": "T1", "agent": None, "text": "first"},
        {"kind": "tool", "phase": "post", "turn_id": None, "agent": {"id": "c1"},
         "name": "read_file", "call_id": "old-call", "input": {}, "output": "old"},
        {"kind": "turn-boundary", "turn_id": "C1", "agent": {"id": "c1"}},
        {"kind": "assistant", "turn_id": "T1", "agent": None, "text": "first done"},
        {"kind": "prompt", "turn_id": "T2", "agent": None, "text": "second"},
        {"kind": "tool", "phase": "post", "turn_id": None, "agent": {"id": "c1"},
         "name": "read_file", "call_id": "new-call", "input": {}, "output": "new"},
        {"kind": "turn-boundary", "turn_id": "C2", "agent": {"id": "c1"}},
        {"kind": "assistant", "turn_id": "T2", "agent": None, "text": "second done"},
        {"kind": "assistant", "turn_id": "C1", "agent": {"id": "c1"},
         "text": "OLD-REPLY-" + "x" * 700},
        {"kind": "assistant", "turn_id": "C2", "agent": {"id": "c1"},
         "text": "NEW-REPLY"},
    ]

    _, trace_text, _ = critic._trace(rows, None, "T2", harness="vibe")
    replies = [item for item in json.loads(trace_text) if item["tool"] == "SUB-AGENT REPORT"]

    old_reply = next(item for item in replies if item["output"].startswith("OLD-REPLY"))
    new_reply = next(item for item in replies if item["output"] == "NEW-REPLY")
    assert old_reply["turn"] == "earlier"
    assert len(old_reply["output"]) == 500
    assert "turn" not in new_reply


def test_late_child_reply_after_next_main_prompt_is_not_in_previous_trace():
    rows = [
        {"kind": "prompt", "turn_id": "T1", "agent": None, "text": "delegate"},
        {"kind": "turn-boundary", "turn_id": "C1", "agent": {"id": "c1"}},
        {"kind": "assistant", "turn_id": "T1", "agent": None, "text": "done"},
        {"kind": "prompt", "turn_id": "T2", "agent": None, "text": "next"},
        {"kind": "assistant", "turn_id": "C1", "agent": {"id": "c1"},
         "text": "too late"},
    ]

    _, trace_text, _ = critic._trace(rows, None, "T1", harness="vibe")

    assert "too late" not in trace_text


def test_claude_does_not_promote_child_reply_after_parent_reply():
    rows = [
        {"kind": "prompt", "turn_id": "T1", "agent": None, "text": "delegate"},
        {"kind": "turn-boundary", "turn_id": "C1", "agent": {"id": "c1"}},
        {"kind": "assistant", "turn_id": "T1", "agent": None, "text": "done"},
        {"kind": "assistant", "turn_id": "C1", "agent": {"id": "c1"},
         "text": "async reply"},
    ]

    _, trace_text, _ = critic._trace(rows, None, "T1", harness="claude")

    assert "async reply" not in trace_text


def test_trace_budget_keeps_current_late_vibe_child_reply():
    rows = [{"kind": "prompt", "turn_id": "T1", "agent": None, "text": "delegate"}]
    rows.extend({
        "kind": "tool", "phase": "post", "turn_id": None, "agent": {"id": "c1"},
        "name": "read_file", "call_id": f"call-{index}", "input": {},
        "output": f"output-{index}-" + "x" * 2000,
    } for index in range(50))
    rows.extend([
        {"kind": "turn-boundary", "turn_id": "C1", "agent": {"id": "c1"}},
        {"kind": "assistant", "turn_id": "T1", "agent": None, "text": "done"},
        {"kind": "assistant", "turn_id": "C1", "agent": {"id": "c1"},
         "text": "LATE-CHILD-REPLY"},
    ])

    _, trace_text, _ = critic._trace(rows, None, "T1", harness="vibe")

    assert len(trace_text) <= critic.MAX_TRACE_CHARS
    assert "LATE-CHILD-REPLY" in trace_text


def test_consecutive_child_turn_windows_use_post_agent_boundaries():
    rows = [
        {"kind": "tool", "phase": "post", "turn_id": None, "agent": {"id": "c1"},
         "name": "read_file", "call_id": "turn1-call", "input": {}, "output": "1" * 1000},
        {"kind": "turn-boundary", "turn_id": "t1", "agent": {"id": "c1"}},
        {"kind": "tool", "phase": "post", "turn_id": None, "agent": {"id": "c1"},
         "name": "read_file", "call_id": "turn2-call", "input": {}, "output": "2" * 1000},
        {"kind": "turn-boundary", "turn_id": "t2", "agent": {"id": "c1"}},
        {"kind": "assistant", "turn_id": "t2", "agent": {"id": "c1"}, "text": "second"},
        {"kind": "assistant", "turn_id": "t1", "agent": {"id": "c1"}, "text": "first"},
    ]

    _, first_trace, _ = critic._trace(rows, "c1", "t1")
    first_items = {item["call_id"]: item for item in json.loads(first_trace)}
    assert "turn" not in first_items["turn1-call"]
    assert "turn2-call" not in first_items

    _, second_trace, _ = critic._trace(rows, "c1", "t2")
    second_items = {item["call_id"]: item for item in json.loads(second_trace)}
    assert second_items["turn1-call"]["turn"] == "earlier"
    assert len(second_items["turn1-call"]["output"]) == 500
    assert "turn" not in second_items["turn2-call"]


def test_large_boot_injection_keeps_every_current_tool_output(tmp_path):
    from compchem_memory.hook.transcript import append_injected

    store = tmp_path / ".magnolia"
    start = HookEvent("vibe", "session-start", "s1", "/project")
    append_injected(
        store, start, channel="boot", text="BOOT HEADER\n" + "A" * 60_000,
        entry_ids=[], tool_call_id=None,
    )
    append_event(store, HookEvent(
        "vibe", "prompt", "s1", "/project", turn_id="T1", prompt="go",
    ))
    for index in range(12):
        append_event(store, HookEvent(
            "vibe", "post-tool", "s1", "/project", turn_id="T1",
            tool=ToolCall("read_file", None, "read_file", f"call-{index}",
                          {"path": "x"}, f"output-{index}-" + "B" * 1980),
        ))
    append_event(store, HookEvent(
        "vibe", "turn-end", "s1", "/project", turn_id="T1",
        assistant_text="report " * 50,
    ))

    _, trace_text, _ = critic._trace(
        read_turns(transcript_path(store, "vibe", "s1")), None, "T1",
    )
    trace = json.loads(trace_text)

    boot = next(item for item in trace if item.get("channel") == "boot")
    assert len(boot["output"]) <= 2000
    assert boot["output"].endswith("... [boot context truncated]")
    assert [item["call_id"] for item in trace if item.get("call_id")] == [
        f"call-{index}" for index in range(12)
    ]
    for index in range(12):
        assert f"output-{index}-" in trace_text


def test_build_trace_includes_delivered_notes_and_denials(tmp_path):
    from compchem_memory.hook.transcript import append_denied, append_injected

    store = tmp_path / ".magnolia"
    event = _event("post-tool", tool=_tool("shell", "denied", tool_input={"cmd": "rm x"}))
    append_denied(store, event, "blocked")
    append_injected(
        store, event, channel="action_memory", text="known recovery", entry_ids=["e1"],
        tool_call_id="note-call",
    )
    append_event(store, _event("turn-end", assistant_text="report"))

    _, trace_text = critic.build_trace(read_turns(transcript_path(store, "claude", "s1")), None)
    assert "DENIED: shell" in trace_text
    assert "MAGNOLIA NOTE" in trace_text
    assert "known recovery" in trace_text


def test_late_stop_judges_its_own_turn(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    route = Route("deepseek", "judge", "https://judge/chat", "key", "openai")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    captured = []
    monkeypatch.setattr(
        critic,
        "call_route",
        lambda route, system, user, **kwargs: (
            captured.append(user)
            or CallResult('{"flag":false,"severity":"none","unsupported_claims":[],"why":"ok"}',
                          "stop", "judge", None, 200)
        ),
    )
    first = "FIRST TURN REPORT " + "a" * 200
    second = "SECOND TURN REPORT " + "b" * 200
    append_event(paths.store, _event("prompt", turn_id="t1", prompt="first"))
    append_event(paths.store, _event("prompt", turn_id="t2", prompt="second"))
    append_event(paths.store, _event("turn-end", turn_id="t1", assistant_text=first))
    append_event(paths.store, _event("turn-end", turn_id="t2", assistant_text=second))

    critic.run_critic(paths, "claude", "s1", None, "t1")

    assert len(captured) == 1
    assert first in captured[0]
    assert second not in captured[0]


def test_turn_trace_excludes_other_turn_rows_after_judged_turn(tmp_path):
    paths = _paths(tmp_path)
    append_event(paths.store, _event("prompt", turn_id="t1", prompt="first"))
    append_event(
        paths.store,
        _event("post-tool", turn_id="t1", tool=_tool("read", "t1-tool", output="first")),
    )
    append_event(paths.store, _event("turn-end", turn_id="t1", assistant_text="first report"))
    append_event(paths.store, _event("prompt", turn_id="t2", prompt="second"))
    append_event(
        paths.store,
        _event("post-tool", turn_id="t2", tool=_tool("read", "t2-tool", output="second")),
    )
    append_event(paths.store, _event("turn-end", turn_id="t2", assistant_text="second report"))

    _, trace_text, _ = critic._trace(
        read_turns(transcript_path(paths.store, "claude", "s1")), None, "t1",
    )

    assert [item["call_id"] for item in json.loads(trace_text)] == ["t1-tool"]


def test_application_probe_keeps_other_turn_pending(tmp_path):
    paths = _paths(tmp_path)
    with SessionState.open(paths.store, "claude", "s1") as state:
        state.data["pending_application"] = [
            {"callID": "c1", "tool": "read", "turn_id": "t1",
             "titles": ["Alpha catalyst"], "paths": ["alpha.md"]},
            {"callID": "c2", "tool": "read", "turn_id": "t2",
             "titles": ["Beta solvent"], "paths": ["beta.md"]},
        ]

    probe_application(paths, "claude", "s1", "I used the alpha catalyst.", "t1")

    rows = [json.loads(line) for line in (paths.store / "action-retrieval.jsonl").read_text().splitlines()]
    assert [row["callID"] for row in rows if row.get("event") == "application"] == ["c1"]
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert [item["callID"] for item in state.data["pending_application"]] == ["c2"]


def test_run_critic_writes_verdict_and_notice(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    route = Route("deepseek", "judge", "https://judge/chat", "key", "openai")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    verdict = '{"flag":true,"severity":"high","unsupported_claims":["ranking"],"why":"no ranking tool output"}'
    monkeypatch.setattr(
        critic, "call_route",
        lambda *args, **kwargs: CallResult(
            f"```json\n{verdict}\n```", "stop", "resolved", None, 200, "DeepInfra",
            {"prompt_tokens": 12, "cached_tokens": 8}, True,
        ),
    )
    append_event(paths.store, _event("prompt", prompt="rank"))
    append_event(paths.store, _event("post-tool", tool=_tool("read", "c1", output="nothing")))
    append_event(paths.store, _event("turn-end", assistant_text="x" * 200))

    outcome = critic.run_critic(paths, "claude", "s1", None)

    row = json.loads((paths.store / "claim-critic" / "s1.jsonl").read_text().strip())
    assert row | {"ts": row["ts"]} == row
    assert row["sessionID"] == "s1" and row["harness"] == "claude" and row["agent"] is None
    assert row["provider"] == "deepseek" and row["model"] == "judge"
    assert row["upstream_provider"] == "DeepInfra"
    assert row["usage"] == {"prompt_tokens": 12, "cached_tokens": 8}
    assert row["quality_floor_dropped"] is True
    assert row["flag"] is True and row["n_tools"] == 1 and row["report_preview"] == "x" * 200
    assert outcome.kind == "verdict" and outcome.reason is None
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["unshown_notices"] == ["⚠ (high) no ranking tool output"]


def test_opencode_flag_stays_in_its_session_and_out_of_job_queue(tmp_path):
    paths = _paths(tmp_path)
    critic._save_notice(paths, "opencode", "s1", {
        "flag": True, "severity": "high", "why": "no ranking tool output",
    })

    assert not (paths.store / ".job-notices.jsonl").exists()
    with SessionState.open(paths.store, "opencode", "s1") as state:
        assert state.data["unshown_notices"] == ["⚠ (high) no ranking tool output"]
    with SessionState.open(paths.store, "opencode", "s2") as state:
        assert state.data.get("unshown_notices") is None


def test_run_critic_is_idempotent_for_one_turn(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    route = Route("deepseek", "judge", "https://judge/chat", "key", "openai")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    calls = []
    verdict = '{"flag":false,"severity":"none","unsupported_claims":[],"why":"supported"}'
    monkeypatch.setattr(
        critic, "call_route",
        lambda *args, **kwargs: calls.append(1) or CallResult(verdict, "stop", "judge", None, 200),
    )
    append_event(paths.store, _event("prompt", turn_id="t1", prompt="rank"))
    append_event(paths.store, _event("turn-end", turn_id="t1", assistant_text="x" * 200))

    critic.run_critic(paths, "claude", "s1", None, "t1")
    critic.run_critic(paths, "claude", "s1", None, "t1")

    assert calls == [1]
    assert len((paths.store / "claim-critic" / "s1.jsonl").read_text().splitlines()) == 1


def test_critic_verdict_is_private_regardless_of_umask(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    route = Route("deepseek", "judge", "https://judge/chat", "key", "openai")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    verdict = '{"flag":false,"severity":"none","unsupported_claims":[],"why":"supported"}'
    monkeypatch.setattr(critic, "call_route", lambda *args, **kwargs: CallResult(verdict, "stop", "resolved", None, 200))
    append_event(paths.store, _event("prompt", prompt="rank"))
    append_event(paths.store, _event("post-tool", tool=_tool("read", "c1", output="nothing")))
    append_event(paths.store, _event("turn-end", assistant_text="x" * 200))

    previous_umask = os.umask(0o022)
    try:
        critic.run_critic(paths, "claude", "s1", None)
    finally:
        os.umask(previous_umask)

    directory = paths.store / "claim-critic"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "s1.jsonl").stat().st_mode & 0o777 == 0o600


def test_run_critic_logs_http_error(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    route = Route("mistral", "judge", "https://judge/chat", "key", "openai")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    monkeypatch.setattr(
        critic, "call_route",
        lambda *args, **kwargs: CallResult(
            None, None, None, "HTTP 401: denied", 401, usage={"prompt_tokens": 9},
            quality_floor_dropped=True,
        ),
    )
    append_event(paths.store, _event("prompt", prompt="rank"))
    append_event(paths.store, _event("turn-end", assistant_text="x" * 200))
    outcome = critic.run_critic(paths, "claude", "s1", None)

    error = json.loads((paths.store / "claim-critic" / "errors.jsonl").read_text().strip())
    assert error["sessionID"] == "s1" and error["provider"] == "mistral"
    assert error["status"] == 401 and error["error"] == "HTTP 401: denied"
    assert error["usage"] == {"prompt_tokens": 9}
    assert error["quality_floor_dropped"] is True
    assert not (paths.store / "claim-critic" / "s1.jsonl").exists()
    assert outcome.kind == "error" and outcome.reason == "HTTP 401: denied"


def test_run_critic_dot_dot_session_id_stays_in_store(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    route = Route("deepseek", "judge", "https://judge/chat", "key", "openai")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    verdict = '{"flag":true,"severity":"high","unsupported_claims":["x"],"why":"no tool output"}'
    monkeypatch.setattr(critic, "call_route", lambda *args, **kwargs: CallResult(verdict, "stop", "resolved", None, 200))
    session = "../../../../escape"
    append_event(paths.store, _event("prompt", session=session, prompt="rank"))
    append_event(paths.store, _event("turn-end", session=session, assistant_text="x" * 200))

    critic.run_critic(paths, "claude", session, None)

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all(paths.store in p.parents for p in written)
    [row_file] = (paths.store / "claim-critic").glob("*.jsonl")
    assert json.loads(row_file.read_text())["sessionID"] == session


def test_route_repr_hides_the_key():
    route = Route("deepseek", "judge", "https://judge/chat", "sk-fabricated-route-key", "openai")
    assert "sk-fabricated-route-key" not in repr(route)
    assert route.key == "sk-fabricated-route-key"


@pytest.mark.parametrize(
    ("enabled", "reply", "route", "reason"),
    [
        (False, "x" * 200, None, "critic disabled"),
        (True, "Done.", Route("openai", "judge", "u", "k", "openai"), "short reply"),
        (True, "x" * 200, None, "no route"),
    ],
)
def test_run_critic_returns_typed_skip_without_judge(
    tmp_path, monkeypatch, enabled, reply, route, reason,
):
    paths = _paths(tmp_path)
    if enabled:
        monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    monkeypatch.setattr(critic, "resolve_route", lambda _role: route)
    calls = []
    monkeypatch.setattr(critic, "_judge", lambda *_args: calls.append(True))
    append_event(paths.store, _event("prompt", prompt="rank"))
    append_event(paths.store, _event("turn-end", assistant_text=reply))

    outcome = critic.run_critic(paths, "claude", "s1", None, "t1")

    assert outcome.kind == "skipped"
    assert outcome.status == "skipped"
    assert outcome.reason == reason
    assert calls == []


def test_late_stop_trace_excludes_the_next_turns_tools(tmp_path):
    """Real late order: turn 2's tools are recorded before turn 1's report."""
    paths = _paths(tmp_path)
    append_event(paths.store, _event("prompt", turn_id="t1", prompt="first"))
    append_event(
        paths.store,
        _event("post-tool", turn_id="t1", tool=_tool("read", "t1-tool", output="first")),
    )
    append_event(paths.store, _event("prompt", turn_id="t2", prompt="second"))
    append_event(
        paths.store,
        _event("post-tool", turn_id="t2", tool=_tool("read", "t2-tool", output="second")),
    )
    append_event(paths.store, _event("turn-end", turn_id="t1", assistant_text="first report"))

    report, trace_text, _ = critic._trace(
        read_turns(transcript_path(paths.store, "claude", "s1")), None, "t1",
    )

    assert report == "first report"
    assert [item["call_id"] for item in json.loads(trace_text)] == ["t1-tool"]


def test_run_critic_malformed_verdict_logs_finish_reason_and_length(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    route = Route("openrouter", "judge", "https://judge/chat", "key", "openai")
    monkeypatch.setattr(critic, "resolve_route", lambda role: route)
    truncated = '{"flag": true, "unsupported_claims": ["one", "two'
    monkeypatch.setattr(
        critic, "call_route",
        lambda *args, **kwargs: CallResult(
            truncated, "length", "judge", None, 200, "DeepInfra",
        ),
    )
    append_event(paths.store, _event("prompt", prompt="rank"))
    append_event(paths.store, _event("turn-end", assistant_text="x" * 200))
    critic.run_critic(paths, "claude", "s1", None)

    error = json.loads((paths.store / "claim-critic" / "errors.jsonl").read_text().strip())
    assert error["error"] == "judge reply has no JSON object"
    assert error["finish_reason"] == "length" and error["reply_chars"] == len(truncated)
    assert error["upstream_provider"] == "DeepInfra"


def _naive_fit_trace(items):
    """The previous quadratic implementation, kept as the reference result."""
    kept = list(items)
    text = json.dumps(kept, ensure_ascii=False, indent=1)
    while len(text) > critic.MAX_TRACE_CHARS and len(kept) > 1:
        kept.pop(0)
        text = json.dumps(kept, ensure_ascii=False, indent=1)
    if len(text) > critic.MAX_TRACE_CHARS and kept:
        kept[0] = {**kept[0], "output": (
            "... [trace item truncated]\n" + str(kept[0].get("output", ""))[-40_000:]
        )}
        text = json.dumps(kept, ensure_ascii=False, indent=1)
    return text


def test_fit_trace_matches_the_reference_on_varied_inputs():
    import random

    rng = random.Random(7)
    for size in (0, 1, 2, 5, 40, 200):
        items = [{"tool": f"t{i}", "input": {"q": "é" * rng.randint(0, 300)},
                  "output": "x\n" * rng.randint(0, 3000)} for i in range(size)]
        assert critic._fit_trace(items) == _naive_fit_trace(items)
    huge = [{"tool": "big", "output": "y" * 200_000}]
    assert len(critic._fit_trace(huge)) <= critic.MAX_TRACE_CHARS


def test_vibe_child_tool_rows_in_its_turn_window_are_current():
    rows = [
        {"kind": "assistant", "turn_id": "old", "agent": {"id": "c1"}, "text": "old"},
        {"kind": "tool", "phase": "post", "turn_id": None, "agent": {"id": "c1"},
         "name": "read_file", "call_id": "current", "input": {}, "output": "x" * 1000},
        {"kind": "assistant", "turn_id": "new", "agent": {"id": "c1"}, "text": "new"},
    ]

    report, trace_text, _ = critic._trace(rows, "c1", "new")

    assert report == "new"
    [item] = json.loads(trace_text)
    assert item["call_id"] == "current"
    assert "turn" not in item
    assert len(item["output"]) == 1000


def test_fit_trace_is_linear_on_thousands_of_items():
    import time

    items = [{"tool": "Read", "input": {"path": "x" * 50}, "output": "B" * 2000}
             for _ in range(4000)]
    started = time.monotonic()
    critic._fit_trace(items)
    assert time.monotonic() - started < 2.0
