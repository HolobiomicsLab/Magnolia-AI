"""claim-critic plugin: judge-provider resolution and request shapes.

Runs the real plugin under Node (type stripping, Node >= 22.6) through its v1
``server()`` hook with ``fetch`` replaced by a recorder, so no network call is
made. Each case sets the environment, triggers one ``session.idle`` and checks
the request the plugin sent and the verdict row it wrote.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / ".opencode" / "plugins" / "claim-critic.ts"
NODE = shutil.which("node")

KEY_VARS = (
    "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "KIMI_PLAN_MAGNOLIA_API_KEY",
    "KIMI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "ZHIPU_API_KEY", "ZAI_API_KEY",
    "DEEPSEEK_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "KIMI_BASE_URL",
    "OPENROUTER_BASE_URL", "MISTRAL_BASE_URL", "ZAI_BASE_URL",
    "MAGNOLIA_CRITIC_PROVIDER", "MAGNOLIA_CRITIC_MODEL", "MAGNOLIA_CRITIC_API_KEY",
    "MAGNOLIA_CRITIC_BASE_URL", "MAGNOLIA_PROJECT_DIR", "MAGNOLIA_PROJECT_WORKSPACE",
)

DRIVER = r"""
const [pluginPath, directory, replyKind, status] = process.argv.slice(2)
const verdict = { flag: true, severity: "low", unsupported_claims: ["x"], why: "no tool ran" }
const calls = []
const timeoutDelays = []
const realSetTimeout = globalThis.setTimeout
globalThis.setTimeout = (callback, delay, ...args) => {
  timeoutDelays.push(delay)
  return realSetTimeout(callback, delay, ...args)
}
const order = []
const realClearTimeout = globalThis.clearTimeout
globalThis.clearTimeout = (handle) => {
  order.push("clear")
  return realClearTimeout(handle)
}
globalThis.fetch = async (url, init) => {
  calls.push({
    url, headers: init.headers, body: JSON.parse(init.body),
    hasSignal: Boolean(init.signal), timeoutDelays: [...timeoutDelays],
  })
  const text = "```json\n" + JSON.stringify(verdict) + "\n```"
  const data = replyKind === "anthropic"
    ? { content: [{ type: "text", text }] }
    : { choices: [{ message: { content: text } }] }
  return {
    ok: Number(status) < 400, status: Number(status),
    json: async () => { order.push("json"); return data },
  }
}
const mod = await import(pluginPath)
const report = "The binding energy is -9.3 kcal/mol and the pose ranks first. ".repeat(5)
const messages = [
  { info: { role: "user" }, parts: [{ type: "text", text: "dock it" }] },
  { info: { role: "assistant" }, parts: [{ type: "text", text: report }] },
]
const client = {
  session: { messages: async () => ({ data: messages }) },
  tui: { showToast: async () => {} },
}
const hooks = await mod.default.server({ client, directory })
if (hooks.event) await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
for (const call of calls) call.order = order
process.stdout.write(JSON.stringify(calls))
"""

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_critic(tmp_path, env, reply_kind="openai", status=200):
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER)
    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("goal\n")
    full_env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    full_env.update({"MAGNOLIA_CRITIC": "1", **env})
    proc = subprocess.run(
        [NODE, str(driver), PLUGIN.as_uri(), str(project), reply_kind, str(status)],
        env=full_env, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in proc.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert proc.returncode == 0, proc.stderr
    calls = json.loads(proc.stdout or "[]")
    out = project / ".magnolia" / "claim-critic"
    rows = [json.loads(l) for l in (out / "s1.jsonl").read_text().splitlines()] if (out / "s1.jsonl").exists() else []
    errors = [json.loads(l) for l in (out / "errors.jsonl").read_text().splitlines()] if (out / "errors.jsonl").exists() else []
    return calls, rows, errors


def test_deepseek_key_alone_keeps_historical_route(tmp_path):
    calls, rows, _ = run_critic(tmp_path, {"DEEPSEEK_API_KEY": "ds"})
    assert calls[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert calls[0]["body"]["model"] == "deepseek-flash"
    assert calls[0]["body"]["thinking"] == {"type": "disabled"}
    assert rows[0]["provider"] == "deepseek" and rows[0]["flag"] is True


def test_deepseek_wins_autodetect_over_later_keys(tmp_path):
    calls, _, _ = run_critic(tmp_path, {"MISTRAL_API_KEY": "mk", "DEEPSEEK_API_KEY": "ds"})
    assert calls[0]["url"].startswith("https://api.deepseek.com/")


def test_mistral_by_model_name(tmp_path):
    calls, rows, _ = run_critic(tmp_path, {
        "MISTRAL_API_KEY": "mk", "DEEPSEEK_API_KEY": "ds",
        "MAGNOLIA_CRITIC_MODEL": "mistral-medium-latest",
    })
    call = calls[0]
    assert call["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer mk"
    assert call["body"]["model"] == "mistral-medium-latest"
    assert "thinking" not in call["body"]
    assert rows[0]["provider"] == "mistral"


def test_zai_coding_plan_endpoint(tmp_path):
    calls, rows, _ = run_critic(tmp_path, {
        "MAGNOLIA_CRITIC_PROVIDER": "zai", "ZHIPU_API_KEY": "zk",
        "ZAI_BASE_URL": "https://api.z.ai/api/coding/paas/v4",
    })
    assert calls[0]["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert calls[0]["body"]["model"] == "glm-5.3-flash"
    assert calls[0]["body"]["thinking"] == {"type": "disabled"}
    assert rows[0]["model"] == "glm-5.3-flash"


def test_anthropic_messages_shape(tmp_path):
    calls, rows, _ = run_critic(tmp_path, {
        "MAGNOLIA_CRITIC_MODEL": "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY": "an",
    }, reply_kind="anthropic")
    call = calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "an"
    assert "Authorization" not in call["headers"]
    assert call["body"]["system"] and "response_format" not in call["body"]
    assert rows[0]["flag"] is True


def test_explicit_key_and_base_url_for_compatible_service(tmp_path):
    calls, _, _ = run_critic(tmp_path, {
        "MAGNOLIA_CRITIC_PROVIDER": "openai", "MAGNOLIA_CRITIC_API_KEY": "own",
        "MAGNOLIA_CRITIC_BASE_URL": "https://llm.example.org/api/", "MAGNOLIA_CRITIC_MODEL": "local-judge",
    })
    assert calls[0]["url"] == "https://llm.example.org/api/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer own"
    assert calls[0]["body"]["model"] == "local-judge"


def test_no_key_makes_no_call(tmp_path):
    calls, rows, errors = run_critic(tmp_path, {})
    assert calls == [] and rows == [] and errors == []


def test_http_error_is_logged_not_raised(tmp_path):
    calls, rows, errors = run_critic(tmp_path, {"MISTRAL_API_KEY": "mk"}, status=401)
    assert len(calls) == 1 and rows == []
    assert errors[0]["provider"] == "mistral" and "401" in errors[0]["error"]


def test_judge_fetch_uses_python_route_timeout(tmp_path):
    calls, rows, errors = run_critic(tmp_path, {"MISTRAL_API_KEY": "mk"})

    assert calls[0]["hasSignal"] is True
    assert calls[0]["timeoutDelays"] == [60_000]
    assert len(rows) == 1 and errors == []


def test_judge_timeout_covers_the_response_body(tmp_path):
    calls, rows, errors = run_critic(tmp_path, {"MISTRAL_API_KEY": "mk"})

    order = calls[0]["order"]
    assert "json" in order and "clear" in order
    assert order.index("json") < order.index("clear")
