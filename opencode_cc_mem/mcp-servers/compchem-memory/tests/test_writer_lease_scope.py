"""The project writer lease covers only short store critical sections.

Lead ruling "lease scope correction" (fix mission B): hooks never block on the
lease, no lease is held across an LLM, network or subprocess call, nothing takes
the lease at import or server start, waiters never use asyncio's default
executor, cancellation never spins, and every store writer (dreaming apply,
run records) takes the same scoped lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

from compchem_memory.writer_lease import (
    acquire_project_writer_lease,
    release_project_writer_lease,
)

PY = sys.executable

_HOLDER = textwrap.dedent(
    """
    import select, sys
    from compchem_memory.writer_lease import (
        acquire_project_writer_lease, release_project_writer_lease,
    )
    _project, descriptor = acquire_project_writer_lease(sys.argv[1])
    print("HELD", flush=True)
    select.select([sys.stdin], [], [], float(sys.argv[2]))
    release_project_writer_lease(descriptor)
    print("RELEASED", flush=True)
    """
)


@contextlib.contextmanager
def _external_holder(project: Path, seconds: float = 10.0):
    """Hold the project's lease from another process for up to ``seconds``."""
    holder = subprocess.Popen(
        [PY, "-c", _HOLDER, str(project), str(seconds)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "HELD"
        yield holder
    finally:
        if holder.stdin and not holder.stdin.closed:
            holder.stdin.close()
        holder.wait(timeout=seconds + 5)


def _release(holder) -> None:
    holder.stdin.close()
    assert holder.stdout.readline().strip() == "RELEASED"


def _lease_is_free(project: Path) -> bool:
    """Probe the lock from a fresh descriptor, as any other writer would."""
    path = project / ".magnolia" / ".writer.lock"
    if not path.exists():
        return True
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)
    return True


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    project = root / "projects" / "alpha"
    (root / "rules").mkdir(parents=True)
    (project / ".magnolia" / "entries").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("Goal\n", encoding="utf-8")
    return project


def _hook_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ, HOME=str(tmp_path / "home"), XDG_STATE_HOME=str(tmp_path / "state"))
    env.pop("MAGNOLIA_HOOK_POLICY", None)
    env.pop("MAGNOLIA_HOOK_POLICY_REQUIRED", None)
    env.update(extra)
    return env


def _run_hook(event: str, row: dict, env: dict[str, str]) -> tuple[float, subprocess.CompletedProcess]:
    started = time.monotonic()
    result = subprocess.run(
        [PY, "-m", "compchem_memory.hook", "json", event], input=json.dumps(row),
        capture_output=True, text=True, timeout=60, env=env,
    )
    return time.monotonic() - started, result


def _tool_row(project: Path, raw_name: str, output: str | None = None) -> dict:
    row = {"session_id": "s1", "cwd": str(project),
           "tool": {"raw_name": raw_name, "call_id": "c1", "input": {"cmd": "ls"}}}
    if output is not None:
        row["tool"]["output"] = output
    return row


# ---- item 1: hooks never block on the lease ---------------------------------

def test_pre_tool_hook_keeps_its_decision_while_lease_is_held(tmp_path):
    project = _workspace(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text('{"deny_tools":["bash"]}', encoding="utf-8")
    allow_env = _hook_env(tmp_path)
    deny_env = _hook_env(tmp_path, MAGNOLIA_HOOK_POLICY=str(policy))
    allow_row = _tool_row(project, "compchem-tools_run_shell")
    deny_row = _tool_row(project, "Bash")
    _free_allow_time, free_allow = _run_hook("pre-tool", allow_row, allow_env)
    _free_deny_time, free_deny = _run_hook("pre-tool", deny_row, deny_env)

    with _external_holder(project, 10.0):
        allow_time, held_allow = _run_hook("pre-tool", allow_row, allow_env)
        deny_time, held_deny = _run_hook("pre-tool", deny_row, deny_env)

    assert allow_time < 1.0, f"allowed pre-tool waited {allow_time:.2f}s behind the lease"
    assert deny_time < 1.0, f"denied pre-tool waited {deny_time:.2f}s behind the lease"
    assert (held_allow.returncode, held_allow.stdout) == (free_allow.returncode, free_allow.stdout)
    assert (held_deny.returncode, held_deny.stderr) == (free_deny.returncode, free_deny.stderr)
    assert held_deny.returncode == 2


def test_post_tool_prompt_and_session_hooks_return_and_record_while_lease_is_held(tmp_path):
    from compchem_memory.hook.transcript import transcript_path

    project = _workspace(tmp_path)
    env = _hook_env(tmp_path)
    store = project / ".magnolia"
    rows = {
        "session-start": {"session_id": "s1", "cwd": str(project)},
        "prompt": {"session_id": "s1", "cwd": str(project), "prompt": "Summarise the project"},
        "post-tool": _tool_row(project, "compchem-tools_run_shell", output="ok"),
    }
    with _external_holder(project, 10.0):
        timings = {event: _run_hook(event, row, env) for event, row in rows.items()}

    for event, (elapsed, result) in timings.items():
        assert elapsed < 1.0, f"{event} hook waited {elapsed:.2f}s behind the lease"
        assert result.returncode == 0, result.stderr
    kinds = [
        json.loads(line).get("kind")
        for line in transcript_path(store, "json", "s1").read_text(encoding="utf-8").splitlines()
    ]
    assert "prompt" in kinds and "tool" in kinds
    assert "s1" in (store / "agent-sessions.jsonl").read_text(encoding="utf-8")


def _background_job(tmp_path: Path, project: Path, harness: str = "vibe") -> Path:
    from compchem_memory.hook.state import SessionState

    store = project / ".magnolia"
    payload = {
        "root": str(tmp_path), "project_dir": str(project), "store": str(store),
        "name": "alpha", "root_mode": False, "harness": harness, "session_id": "s1",
    }
    job = store / "hook-workers" / "job.json"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(json.dumps(payload), encoding="utf-8")
    with SessionState.open(store, harness, "s1") as state:
        state.data["background_queue"] = [{
            "agent_id": None, "turn_id": "t1", "reply_text": "final reply",
            "key": "main--t1", "queued": 1.0,
        }]
        state.data["background_turns"] = {"main--t1": {"status": "queued", "attempts": 0}}
        state.data["background_worker"] = {"status": "running", "started": 1.0}
    return job


def test_background_critic_never_holds_the_lease_and_lands_its_write(tmp_path, monkeypatch):
    from compchem_memory.hook import action_retrieval, background, critic
    from compchem_memory.hook.state import SessionState

    project = _workspace(tmp_path)
    store = project / ".magnolia"
    observed = []

    def fake_critic(paths, harness, session_id, agent_id, turn_id):
        observed.append(_lease_is_free(project))
        out = paths.store / "claim-critic" / f"{session_id}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"turn_id": turn_id}) + "\n", encoding="utf-8")

    monkeypatch.setattr(action_retrieval, "probe_application", lambda *args: None)
    monkeypatch.setattr(critic, "run_critic", fake_critic)

    background.run_job(_background_job(tmp_path, project))
    assert observed == [True], "the critic (an LLM call) ran under the project lease"

    observed.clear()
    with SessionState.open(store, "vibe", "s1") as state:
        state.data.clear()
    with _external_holder(project, 10.0):
        worker = threading.Thread(target=background.run_job, args=(_background_job(tmp_path, project),))
        started = time.monotonic()
        worker.start()
        worker.join(5.0)
        assert not worker.is_alive(), "the background worker waited on an unrelated holder"
    assert time.monotonic() - started < 5.0
    assert (store / "claim-critic" / "s1.jsonl").exists()


def test_opencode_worker_exports_and_judges_outside_the_lease(tmp_path, monkeypatch):
    from compchem_memory import opencode_ingest
    from compchem_memory.hook import background, critic

    project = _workspace(tmp_path)
    store = project / ".magnolia"
    folder = store / "hook-workers"
    folder.mkdir(parents=True)
    payload = {
        "root": str(tmp_path), "project_dir": str(project), "store": str(store),
        "name": "alpha", "root_mode": False, "harness": "opencode",
        "operation": "critic", "session_id": "s1", "turn_id": "u1",
    }
    job = folder / "opencode-turn-1.json"
    job.write_text(json.dumps(payload), encoding="utf-8")
    exported = {"messages": [
        {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "check"}]},
        {"info": {"role": "assistant", "id": "a1"}, "parts": [{"type": "text", "text": "done" * 80}]},
    ]}
    observed = []

    def exporter(_sid):
        observed.append(("export", _lease_is_free(project)))
        return exported

    def judge(paths, harness, session_id, agent_id, turn_id):
        observed.append(("critic", _lease_is_free(project)))
        out = paths.store / "claim-critic" / f"{session_id}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"turn_id": turn_id}) + "\n", encoding="utf-8")

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: exporter)
    monkeypatch.setattr(critic, "run_critic", judge)

    background.run_opencode_job(job)

    assert observed == [("export", True), ("critic", True)]
    assert json.loads((store / "opencode-transcripts" / "s1.json").read_text())["messages"]


# ---- item 3: nothing takes the lease at import or server start --------------

def _stdio_transport(project: Path, tmp_path: Path, tools: tuple[str, ...]):
    from fastmcp.client.transports import StdioTransport

    from compchem_memory import memory_http
    from compchem_memory.tool_catalog import MEMORY_TOOL_PREFIX

    runner = Path(memory_http.__file__).with_name("hook") / "runner.py"
    allowed = [f"{MEMORY_TOOL_PREFIX}{name}" for name in tools]
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "MAGNOLIA_PROJECT_DIR": str(project),
        "MAGNOLIA_DISTILL_INTERVAL_MIN": "600",
        "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
    }
    return StdioTransport(
        command=PY,
        args=["-I", str(runner), "--memory-server", "--tool-allowlist-json", json.dumps(allowed)],
        env=env, cwd=str(project),
    )


@pytest.mark.asyncio
async def test_memory_server_starts_and_reads_while_lease_is_held(tmp_path):
    from fastmcp import Client

    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    transport = _stdio_transport(
        project, tmp_path, ("memory_get_goal", "memory_get_context", "memory_set_goal"),
    )
    with _external_holder(project, 10.0) as holder:
        started = time.monotonic()
        async with Client(transport, timeout=8) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"memory_get_goal", "memory_get_context", "memory_set_goal"}
            read = await client.call_tool("memory_get_goal", {"project_dir": str(project)})
            assert read.is_error is False
            context = await client.call_tool(
                "memory_get_context",
                {"task_description": "lease probe", "project_dir": str(project)},
            )
            assert context.is_error is False
            assert time.monotonic() - started < 8.0
            write = asyncio.create_task(client.call_tool(
                "memory_set_goal", {"goal": "after the holder", "project_dir": str(project)},
            ))
            await asyncio.sleep(0.3)
            assert not write.done(), "a store write did not wait for the project lease"
            _release(holder)
            result = await asyncio.wait_for(write, timeout=5)
            assert result.is_error is False
    assert (project / ".magnolia" / "GOAL.md").read_text().strip() == "after the holder"


@pytest.mark.asyncio
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores mode bits")
async def test_unwritable_project_boots_read_only(tmp_path):
    from fastmcp import Client

    project = tmp_path / "project"
    project.mkdir()
    project.chmod(0o555)
    try:
        transport = _stdio_transport(project, tmp_path, ("memory_get_goal",))
        async with Client(transport, timeout=8) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"memory_get_goal"}
    finally:
        project.chmod(0o755)
    assert not (project / ".magnolia").exists()


def test_writer_on_unwritable_project_is_not_a_lease_error(tmp_path):
    from compchem_memory.writer_lease import project_writer_lease

    project = tmp_path / "project"
    project.mkdir()
    project.chmod(0o555)
    try:
        with project_writer_lease(project):
            entered = True
    finally:
        project.chmod(0o755)
    assert entered


# ---- item 2: no lease across LLM, network or subprocess calls ---------------

def test_boot_pipeline_and_timer_hold_no_lease_around_their_steps(tmp_path, monkeypatch):
    from compchem_memory import server, smoke

    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    observed = []
    done = threading.Event()

    def step():
        observed.append(("boot", _lease_is_free(project)))
        done.set()

    monkeypatch.setattr(server, "PROJECT_DIR", str(project))
    monkeypatch.setattr(server, "_build_boot_steps", lambda _project: [("probe", step)])
    monkeypatch.delenv(f"MAGNOLIA_ONCE_boot_{abs(hash(str(project)))}", raising=False)
    server._run_startup_scan_background()
    assert done.wait(10)

    monkeypatch.setattr(smoke, "maybe_run_scheduled", lambda _project: observed.append(
        ("smoke", _lease_is_free(project))))
    monkeypatch.setattr(server, "scan_and_distill", lambda _project: observed.append(
        ("sweep", _lease_is_free(project))))
    monkeypatch.setenv("MAGNOLIA_OFFPEAK_DISABLE", "1")
    server._distill_timer_tick(str(project))

    assert observed == [("boot", True), ("smoke", True), ("sweep", True)]


def test_session_append_waits_for_compaction_swap_and_survives(tmp_path, monkeypatch):
    from compchem_memory import compaction
    from compchem_memory.writer_lease import file_fingerprint

    project = tmp_path / "project"
    sessions = project / ".magnolia" / "sessions"
    sessions.mkdir(parents=True)
    session = sessions / "active.jsonl"
    session.write_text(json.dumps({"event_type": "session_start"}) + "\n")
    ready = tmp_path / "append-ready"
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from compchem_memory.tiers.session import SessionManager

        sessions, session, ready = map(Path, sys.argv[1:])
        manager = SessionManager(sessions, project_id="project", project_dir=str(sessions.parent.parent))
        manager._current_session = session
        manager._current_session_id = session.stem
        ready.write_text("ready")
        manager.record("sentinel", {"value": "preserved"})
        """
    )
    processes = []
    original_write = compaction._write_compacted_session

    def write_then_race(path, result):
        original_write(path, result)
        process = subprocess.Popen(
            [PY, "-c", script, str(sessions), str(session), str(ready)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.2)

    monkeypatch.setattr(compaction, "_write_compacted_session", write_then_race)
    result = compaction.CompactionResult(
        kept_messages=[], summary="summary", pruned_count=1,
        tokens_before=10, tokens_after=1,
    )

    assert compaction._replace_if_unchanged(
        session, file_fingerprint([session]), result,
    ) is result
    stdout, stderr = processes[0].communicate(timeout=5)

    assert processes[0].returncode == 0, (stdout, stderr)
    active = session.read_text(encoding="utf-8")
    assert '"event_type": "sentinel"' in active
    assert '"value": "preserved"' in active


def _staging_finding(staging: Path, name: str, title: str, session: str) -> Path:
    meta = {"title": title, "type": "scientific_finding", "opencode_session_id": session,
            "observed_in_sessions": [session], "tags": [], "tools": [], "confidence": 0.6}
    path = staging / name
    path.write_text("---\n" + yaml.dump(meta) + "---\n\nThe same finding body.\n")
    return path


def test_consolidation_clusters_outside_the_lease_then_revalidates(tmp_path, capsys):
    from compchem_memory.consolidation import consolidate_project_findings

    project = tmp_path / "project"
    staging = project / ".magnolia" / "staging"
    staging.mkdir(parents=True)
    first = _staging_finding(staging, "a.md", "Alpha finding", "s1")
    _staging_finding(staging, "b.md", "Alpha finding again", "s2")
    observed = []

    def clusterer(payload):
        observed.append(_lease_is_free(project))
        return [{"ids": [row["id"] for row in payload], "confidence": 0.95, "rationale": "same"}]

    result = consolidate_project_findings(str(project / ".magnolia"), clusterer=clusterer)
    artifact = project / ".magnolia" / "reflex" / "consolidation-proposal.json"
    assert observed == [True], "the clusterer (an LLM call) ran under the project lease"
    assert result["clusters"] == 1 and artifact.exists()

    artifact.unlink()

    def racing_clusterer(payload):
        first.write_text(first.read_text() + "\nA concurrent observation.\n")
        return clusterer(payload)

    raced = consolidate_project_findings(str(project / ".magnolia"), clusterer=racing_clusterer)
    assert raced.get("skipped") == "store changed during clustering"
    assert not artifact.exists()
    assert "store changed during clustering" in capsys.readouterr().err


def test_promotion_judges_and_drafts_outside_the_lease(tmp_path):
    from compchem_memory.promotion import propose_promotions

    project = tmp_path / "project"
    entries = project / ".magnolia" / "entries"
    entries.mkdir(parents=True)
    meta = {"title": "Alpha rule", "type": "success_pattern",
            "observed_in_sessions": ["s1", "s2", "s3"], "confidence": 0.9}
    (entries / "a.md").write_text("---\n" + yaml.dump(meta) + "---\n\nuse alpha\n")
    observed = []

    def judge(entry, lens):
        observed.append(_lease_is_free(project))
        return {"approve": True, "correctness_concern": None, "generality_concern": None}

    def drafter(entry):
        observed.append(_lease_is_free(project))
        return {"name": "alpha", "description": "d", "tags": [], "body": "B"}

    def checker(drafted, rules):
        observed.append(_lease_is_free(project))
        return {"status": "ok", "related_rule": None, "note": ""}

    result = propose_promotions(
        str(project / ".magnolia"), rules_dir=str(tmp_path / "rules"),
        judge=judge, drafter=drafter, checker=checker,
    )
    assert result["candidates"] == 1
    assert observed and all(observed), "promotion LLM calls ran under the project lease"


def test_handover_merges_outside_the_lease_then_revalidates(tmp_path, capsys):
    from compchem_memory import handover as hv

    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    (store / "opencode-sessions.jsonl").write_text(
        json.dumps({"opencode_session_id": "ses_a", "ts": "0"}) + "\n")
    export = {"info": {"id": "ses_a"}, "messages": [
        {"info": {"id": "m1", "role": "user"}, "parts": [{"type": "text", "text": "dock"}]},
        {"info": {"id": "m2", "role": "assistant"}, "parts": [{"type": "text", "text": "best"}]},
    ]}
    observed = []

    def llm(system, user, max_tokens=2000, **kwargs):
        observed.append(_lease_is_free(project))
        return "## Done\n- docked\n"

    assert hv.generate_handover(str(project), exporter=lambda _s: export, llm=llm)
    assert observed == [True], "the handover merge (an LLM call) ran under the project lease"

    (store / hv.HANDOVER_CURSORS_DIR / "ses_a.json").unlink()
    before = (store / hv.HANDOVER_STATE_FILE).read_text()

    def racing_llm(system, user, max_tokens=2000, **kwargs):
        (store / hv.HANDOVER_STATE_FILE).write_text(before + "\n- concurrent writer\n")
        return "## Done\n- docked twice\n"

    assert hv.generate_handover(str(project), exporter=lambda _s: export, llm=racing_llm) is None
    assert "docked twice" not in (store / hv.HANDOVER_STATE_FILE).read_text()
    assert not (store / hv.HANDOVER_CURSORS_DIR / "ses_a.json").exists()
    assert "changed during the merge" in capsys.readouterr().err


def test_opencode_ingest_distills_outside_the_lease_then_revalidates(tmp_path, capsys):
    from compchem_memory.opencode_ingest import ingest_opencode_sessions

    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    mapping = store / "opencode-sessions.jsonl"
    mapping.write_text(json.dumps({"opencode_session_id": "ses_a"}) + "\n")
    export = {"messages": [
        {"info": {"id": "m1", "role": "user"}, "parts": [{"type": "text", "text": "dock it"}]},
        {"info": {"id": "m2", "role": "assistant"}, "parts": [{"type": "text", "text": "done"}]},
    ]}
    observed = []

    def distiller(transcript):
        observed.append(_lease_is_free(project))
        return [{"title": "Docking lesson", "content": "use cluster 2", "type": "note"}]

    saved = ingest_opencode_sessions(
        str(store), str(mapping), exporter=lambda _sid: export, distiller=distiller,
    )
    assert observed == [True], "the distiller (an LLM call) ran under the project lease"
    assert len(saved) == 1

    progress = store / "opencode-distilled" / "ses_a.json"
    progress.unlink()

    def racing_distiller(transcript):
        progress.write_text(json.dumps({"cursor": "m2", "candidates": 1, "done": False}) + "\n")
        return distiller(transcript)

    assert ingest_opencode_sessions(
        str(store), str(mapping), exporter=lambda _sid: export, distiller=racing_distiller,
    ) == []
    assert json.loads(progress.read_text())["cursor"] == "m2"
    assert "progress changed during distillation" in capsys.readouterr().err


def test_tool_event_extraction_distills_outside_the_lease(tmp_path, monkeypatch):
    from compchem_memory import extraction

    project = tmp_path / "project"
    sessions = project / ".magnolia" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "s1.jsonl"
    log.write_text(json.dumps({"timestamp": "t1", "event_type": "tool_call", "tool": "x"}) + "\n")
    observed = []

    def generate(self, events):
        observed.append(_lease_is_free(project))
        return [{"title": "Lesson", "content": "body", "type": "note"}]

    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "_generate_candidates", generate)
    saved = extraction.AutomaticMemoryExtractor(str(project)).commit(log, str(project))
    assert observed == [True]
    assert len(saved) == 1


def test_mutating_tool_runs_inline_extraction_outside_its_lease(tmp_path, monkeypatch):
    from compchem_memory import capture, extraction

    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    monkeypatch.delenv("MAGNOLIA_DISABLE_INLINE_EXTRACT", raising=False)
    observed = []
    done = threading.Event()

    def commit(self, session_path, project_dir):
        observed.append(_lease_is_free(project))
        done.set()
        return []

    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "should_extract", lambda *_a: True)
    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "commit", commit)

    @capture.captured(source="compchem-memory")
    def memory_set_goal(goal: str, project_dir: str | None = None) -> str:
        observed.append(not _lease_is_free(project))
        return "ok"

    assert memory_set_goal("goal", project_dir=str(project)) == "ok"
    assert done.wait(5)
    assert observed == [True, True], "inline extraction (an LLM call) ran under the lease"


def test_llm_backed_tools_are_not_leased_as_a_whole():
    from compchem_memory.tool_catalog import memory_tool_requires_lease

    for name in (
        "memory_get_context", "memory_extract_from_session",
        "memory_compact_session", "memory_distill_session",
    ):
        assert memory_tool_requires_lease(name) is False, name
    for name in ("memory_set_goal", "memory_record_learning", "memory_record_run"):
        assert memory_tool_requires_lease(name) is True, name


def test_cli_write_waits_for_the_lease_without_deadlock(tmp_path):
    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    with _external_holder(project, 10.0) as holder:
        cli = subprocess.Popen(
            [PY, "-m", "compchem_memory.cli", "assess", "--project-dir", str(project),
             "--command", "gnina --receptor r.pdb", "--exit-code", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(project),
        )
        time.sleep(1.0)
        assert cli.poll() is None, "the CLI run record did not wait for the lease"
        _release(holder)
        out, err = cli.communicate(timeout=20)
    assert cli.returncode == 0, err
    assert list((project / ".magnolia" / "runs").glob("*.yaml"))


# ---- item 4: no default executor, no spin -----------------------------------

_WAITERS = textwrap.dedent(
    """
    import asyncio, sys, threading, time
    from pathlib import Path
    from fastmcp.server.middleware import MiddlewareContext
    from mcp import types as mt
    from compchem_memory import memory_http

    middleware = memory_http.ProjectWriterLeaseMiddleware(Path(sys.argv[1]))
    context = MiddlewareContext(
        message=mt.CallToolRequestParams(name="memory_set_goal", arguments={}),
        method="tools/call",
    )
    active = maximum = 0
    guard = threading.Lock()

    def body():
        global active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.005)
        with guard:
            active -= 1

    async def main():
        gate = asyncio.Event()

        async def hold(_context):
            await gate.wait()
            return "holder"

        async def write(_context):
            await asyncio.to_thread(body)
            return "writer"

        holder = asyncio.create_task(middleware.on_call_tool(context, hold))
        await asyncio.sleep(0.2)
        waiters = [
            asyncio.create_task(middleware.on_call_tool(context, write)) for _ in range(32)
        ]
        await asyncio.sleep(0.5)
        gate.set()
        done, pending = await asyncio.wait([holder, *waiters], timeout=20)
        print(len(done), len(pending), maximum, flush=True)

    asyncio.run(main())
    """
)


def test_thirty_two_waiters_and_a_holder_do_not_deadlock(tmp_path):
    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    try:
        result = subprocess.run(
            [PY, "-c", _WAITERS, str(project)], capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("32 lease waiters plus a holder deadlocked the service")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["33", "0", "1"]
    assert _lease_is_free(project)


def test_anyio_cancellation_neither_spins_nor_leaks_the_lease(tmp_path):
    import anyio
    from fastmcp.server.middleware import MiddlewareContext
    from mcp import types as mt

    from compchem_memory import memory_http

    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    middleware = memory_http.ProjectWriterLeaseMiddleware(project)
    context = MiddlewareContext(
        message=mt.CallToolRequestParams(name="memory_set_goal", arguments={}),
        method="tools/call",
    )
    active = maximum = 0
    guard = threading.Lock()

    def body(seconds):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(seconds)
        with guard:
            active -= 1
        return "done"

    async def cancelled_after(delay, call):
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as group:
                async def cancel():
                    await anyio.sleep(delay)
                    scope.cancel()

                group.start_soon(cancel)
                await call()

    async def main():
        cpu = time.process_time()
        await cancelled_after(0.1, lambda: middleware.on_call_tool(
            context, lambda _c: anyio.to_thread.run_sync(body, 1.0)))
        write_cpu = time.process_time() - cpu
        assert _lease_is_free(project)

        _project, descriptor = acquire_project_writer_lease(project)
        threading.Timer(1.0, release_project_writer_lease, args=(descriptor,)).start()
        cpu, wall = time.process_time(), time.monotonic()
        await cancelled_after(0.1, lambda: middleware.on_call_tool(
            context, lambda _c: anyio.to_thread.run_sync(body, 0.01)))
        wait_cpu, wait_wall = time.process_time() - cpu, time.monotonic() - wall
        await anyio.sleep(1.1)
        assert _lease_is_free(project)
        later = await middleware.on_call_tool(
            context, lambda _c: anyio.to_thread.run_sync(body, 0.01))
        return write_cpu, wait_cpu, wait_wall, later

    write_cpu, wait_cpu, wait_wall, later = anyio.run(main)
    assert write_cpu < 0.3, f"cancelled write spun the loop ({write_cpu:.2f}s CPU)"
    assert wait_cpu < 0.3, f"cancelled waiter spun the loop ({wait_cpu:.2f}s CPU)"
    assert wait_wall < 0.5, "a cancelled waiter kept waiting for the lease"
    assert later == "done" and maximum == 1


# ---- item 5: the remaining store writers take the same lease ----------------

def _assert_waits_for_lease(project: Path, call) -> None:
    _project, descriptor = acquire_project_writer_lease(project)
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as error:  # noqa: BLE001 - re-raised below
            outcome["error"] = error

    worker = threading.Thread(target=run)
    try:
        worker.start()
        worker.join(0.4)
        assert worker.is_alive(), "the store writer did not wait for the project lease"
    finally:
        release_project_writer_lease(descriptor)
    worker.join(10)
    assert not worker.is_alive()
    if "error" in outcome:
        raise outcome["error"]


def test_dreaming_apply_waits_for_the_lease(tmp_path):
    from compchem_memory.dreaming import DreamingConfig, apply_dreaming

    project = tmp_path / "project"
    (project / ".magnolia" / "reflex").mkdir(parents=True)
    artifact = {"id": "p1", "proposals": [], "applied": [], "dismissed": [], "config": {}}
    _assert_waits_for_lease(
        project, lambda: apply_dreaming(project, artifact=artifact, config=DreamingConfig(commit=False)),
    )


@pytest.mark.parametrize("method", [
    "record_run", "update_run", "upsert_run", "decrement_confidence_for_tool",
])
def test_run_record_writers_wait_for_the_lease(tmp_path, method):
    from compchem_memory.tiers.project import ProjectManager

    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    manager = ProjectManager(tmp_path / "global")
    manager.record_run(str(project), "run-1", "gnina", "pass")
    calls = {
        "record_run": lambda: manager.record_run(
            project_dir=str(project), run_id="run-2", tool="gnina", status="pass"),
        "update_run": lambda: manager.update_run(str(project), "run-1", {"lifecycle": "done"}),
        "upsert_run": lambda: manager.upsert_run(str(project), "run-1", "gnina", status="fail"),
        "decrement_confidence_for_tool": lambda: manager.decrement_confidence_for_tool(
            str(project), "gnina"),
    }
    _assert_waits_for_lease(project, calls[method])
