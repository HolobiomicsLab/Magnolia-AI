import errno
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.executors.contracts import RunOutcome  # noqa: E402
from campaign_runner.governor import Governor, Policy  # noqa: E402
from campaign_runner import process as process_module  # noqa: E402
from campaign_runner.process import ProcessControl, run_process  # noqa: E402
from campaign_runner.routing import ResolvedRoute  # noqa: E402
from campaign_runner.workers import (  # noqa: E402
    InvocationGovernance,
    OpenCodeExecutor,
    WorkerError,
)


def _die_during_pre_exec(tmp_path_text):
    tmp_path = Path(tmp_path_text)

    def kill_parent(pid, _pgid):
        (tmp_path / "child.pid").write_text(str(pid), encoding="utf-8")
        os.kill(os.getpid(), signal.SIGKILL)

    run_process(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(tmp_path / 'client-ran')!r}).write_text('ran')",
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(on_start=kill_parent, hold_before_exec=True),
    )


def _wait_until_process_gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        stat_path = Path(f"/proc/{pid}/stat")
        try:
            fields = stat_path.read_bytes().rsplit(b")", 1)[1].split()
            if fields and fields[0] == b"Z":
                return True
        except FileNotFoundError:
            try:
                status = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(pid)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={"LC_ALL": "C", "TZ": "UTC"},
                )
            except OSError:
                pass
            else:
                if status.returncode == 0 and "Z" in status.stdout:
                    return True
        except (OSError, IndexError):
            pass
        time.sleep(0.02)
    return False


def _open_spare_fds(through_fd):
    held = []
    while not held or held[-1] < through_fd:
        held.append(os.open(os.devnull, os.O_RDONLY))
    return held


def _open_fds(limit=256):
    opened = set()
    for fd in range(limit):
        try:
            os.fstat(fd)
        except OSError:
            continue
        opened.add(fd)
    return opened


def _run_captured(tmp_path, stem, argv, env, governed):
    control = None
    if governed:
        control = ProcessControl(
            on_start=lambda _pid, _pgid: None,
            hold_before_exec=True,
        )
    outcome = run_process(
        argv,
        cwd=tmp_path,
        env=env,
        timeout_seconds=30,
        stdout_path=tmp_path / f"{stem}.out",
        stderr_path=tmp_path / f"{stem}.err",
        control=control,
    )
    return (
        outcome,
        (tmp_path / f"{stem}.out").read_bytes(),
        (tmp_path / f"{stem}.err").read_bytes(),
    )


@pytest.mark.parametrize("governed", (False, True))
def test_client_process_launches_disconnect_stdin(tmp_path, monkeypatch, governed):
    real_popen = subprocess.Popen
    calls = []

    def recording_popen(*args, **kwargs):
        calls.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(process_module.subprocess, "Popen", recording_popen)
    outcome, _stdout, _stderr = _run_captured(
        tmp_path,
        f"stdin-{governed}",
        [sys.executable, "-c", "pass"],
        dict(os.environ),
        governed,
    )

    assert outcome.returncode == 0
    assert calls
    assert all(call.get("stdin") is subprocess.DEVNULL for call in calls)


def _reap_test_process(pid):
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return
    if waited:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def test_run_process_stops_on_callback(tmp_path):
    started = time.monotonic()
    counter = {"n": 0}

    def should_stop():
        counter["n"] += 1
        return "halt requested" if counter["n"] >= 2 else None

    outcome = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(should_stop=should_stop, poll_interval=0.05),
    )
    assert outcome.stopped_reason == "halt requested"
    assert outcome.timed_out is False
    assert time.monotonic() - started < 5


def test_run_process_reports_stop_even_after_fast_exit(tmp_path):
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return None if calls["n"] == 1 else "halt requested"

    outcome = run_process(
        ["/usr/bin/true"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(should_stop=should_stop, poll_interval=0.01),
    )
    assert outcome.stopped_reason == "halt requested"
    assert outcome.returncode == 0


def test_run_process_reports_timeout(tmp_path):
    outcome = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=0.3,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    assert outcome.timed_out is True
    assert outcome.stopped_reason is None
    assert outcome.outcome.kind == "timeout"


def test_run_process_quota_observation_is_typed_and_stops(tmp_path):
    observed = {"calls": 0}

    def observe(_stdout, _stderr):
        observed["calls"] += 1
        return RunOutcome("quota") if observed["calls"] >= 2 else None

    outcome = run_process(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(observe=observe, poll_interval=0.05),
    )
    assert outcome.outcome.kind == "quota"
    assert outcome.stopped_reason == "quota or rate limit"
    assert (tmp_path / "out.log").read_text().strip() == "partial"


def test_run_process_cancel_is_typed(tmp_path):
    outcome = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(should_stop=lambda: "paused", poll_interval=0.01),
    )
    assert outcome.outcome.kind == "cancelled"


def test_governed_process_is_recorded_before_client_runs(tmp_path):
    marker = tmp_path / "client-ran"
    callback_observations = []

    def on_start(_pid, _pgid):
        time.sleep(0.1)
        callback_observations.append(marker.exists())

    outcome = run_process(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(on_start=on_start, hold_before_exec=True),
    )
    assert outcome.returncode == 0
    assert callback_observations == [False]
    assert marker.read_text() == "ran"


def test_governed_pipe_preserves_exec_contract(tmp_path, monkeypatch):
    client_dir = tmp_path / "dir with spaces"
    client_dir.mkdir()
    client = client_dir / "inspect client"
    client.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "fds = []\n"
        "for fd in range(3, 64):\n"
        "    try: os.fstat(fd)\n"
        "    except OSError: continue\n"
        "    fds.append(fd)\n"
        "print(json.dumps({'argv_bytes': [os.fsencode(value).hex() for value in sys.argv[1:]], "
        "'argv0_bytes': os.fsencode(sys.argv[0]).hex(), "
        "'env': dict(os.environ), 'pid': os.getpid(), "
        "'pgid': os.getpgrp(), 'sid': os.getsid(0), 'fds': fds}))\n"
        "print('inherited stderr', file=sys.stderr)\n",
        encoding="utf-8",
    )
    client.chmod(0o755)
    client_link = client_dir / "inspect client link"
    client_link.symlink_to(client)
    canary = tmp_path / "injected"
    hostile = [
        f"$(touch {canary})",
        f"`touch {canary}`",
        f'"; touch {canary}; "',
        "$@",
        "$0",
        "line one\nline two",
        "",
        "*",
        "éè unicode",
    ]
    child_env = {"LC_ALL": "C", "MAGNOLIA_SENTINEL": "exact environment"}
    if "__CF_USER_TEXT_ENCODING" in os.environ:
        child_env["__CF_USER_TEXT_ENCODING"] = os.environ[
            "__CF_USER_TEXT_ENCODING"
        ]
    spare_fds = _open_spare_fds(12)
    barrier_fds = []
    real_pipe = process_module.os.pipe

    def record_pipe():
        opened = real_pipe()
        barrier_fds.append(opened)
        return opened

    monkeypatch.setattr(process_module.os, "pipe", record_pipe)
    try:
        outcome = run_process(
            [str(client_link), *hostile],
            cwd=tmp_path,
            env=child_env,
            timeout_seconds=30,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            control=ProcessControl(
                on_start=lambda _pid, _pgid: None,
                hold_before_exec=True,
            ),
        )
    finally:
        for fd in spare_fds:
            os.close(fd)
    payload = json.loads((tmp_path / "out.log").read_text(encoding="utf-8"))
    assert outcome.returncode == 0
    assert barrier_fds[0][0] >= 10
    assert payload["argv_bytes"] == [os.fsencode(value).hex() for value in hostile]
    assert payload["argv0_bytes"] == os.fsencode(str(client_link)).hex()
    assert payload["env"] == child_env
    assert "PWD" not in payload["env"] and "SHLVL" not in payload["env"]
    assert payload["pid"] == payload["pgid"] == payload["sid"]
    assert payload["fds"] == []
    assert (tmp_path / "err.log").read_text(encoding="utf-8").strip() == "inherited stderr"
    assert not canary.exists()

    exited = run_process(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout_seconds=30,
        stdout_path=tmp_path / "exit.out",
        stderr_path=tmp_path / "exit.err",
        control=ProcessControl(on_start=lambda _pid, _pgid: None, hold_before_exec=True),
    )
    signalled = run_process(
        [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout_seconds=30,
        stdout_path=tmp_path / "signal.out",
        stderr_path=tmp_path / "signal.err",
        control=ProcessControl(on_start=lambda _pid, _pgid: None, hold_before_exec=True),
    )
    assert exited.returncode == 3
    assert signalled.returncode == -signal.SIGTERM


@pytest.mark.parametrize(
    "child_env",
    [
        {"PATH": "/usr/bin:/bin", "SENTINEL": "no locale"},
        {"PATH": "/usr/bin:/bin", "LC_CTYPE": "C", "SENTINEL": "locale"},
        {
            "PATH": "/usr/bin:/bin",
            "__PYVENV_LAUNCHER__": "/x/python",
            "SENTINEL": "launcher",
        },
        {
            "PATH": "/usr/bin:/bin",
            "MAGNOLIA_BARRIER_ORIGINAL_ENV": "caller value",
        },
        {"PATH": "/usr/bin:/bin", "SENTINEL": "café / λ / 🧪"},
        {b"PATH": b"/usr/bin:/bin", b"SENTINEL": b"bytes key and value"},
    ],
    ids=[
        "no-lc-all",
        "lc-ctype-c",
        "pyvenv-launcher",
        "snapshot-marker",
        "non-ascii-environment",
        "bytes-environment",
    ],
)
def test_governed_client_environment_matches_plain_popen(tmp_path, child_env):
    governed = _run_captured(
        tmp_path, "governed-env", ["/usr/bin/env"], child_env, True
    )
    plain = _run_captured(tmp_path, "plain-env", ["/usr/bin/env"], child_env, False)

    assert governed[0].returncode == plain[0].returncode == 0
    assert sorted(governed[1].splitlines()) == sorted(plain[1].splitlines())
    assert governed[2] == plain[2] == b""


def test_governed_large_environment_uses_partial_write_loop(tmp_path, monkeypatch):
    child_env = {"PATH": "/usr/bin:/bin", "BIG": "x" * (100 * 1024)}
    plain = _run_captured(
        tmp_path, "plain-large-one-var", ["/usr/bin/env"], child_env, False
    )
    real_write = process_module.os.write
    write_sizes = []

    def partial_write(fd, body):
        written = real_write(fd, body[:4096])
        write_sizes.append(written)
        return written

    monkeypatch.setattr(process_module.os, "write", partial_write)
    governed = _run_captured(
        tmp_path, "governed-large-one-var", ["/usr/bin/env"], child_env, True
    )

    assert governed[0].returncode == plain[0].returncode == 0
    assert governed[1] == plain[1]
    assert governed[2] == plain[2] == b""
    assert sum(write_sizes) > 64 * 1024
    assert len(write_sizes) > 1


def test_governed_total_environment_near_600_kib_matches_plain(tmp_path):
    child_env = {"PATH": "/usr/bin:/bin"}
    child_env.update({f"V{index:04d}": "y" * 1000 for index in range(600)})

    plain = _run_captured(
        tmp_path, "plain-large-total", ["/usr/bin/env"], child_env, False
    )
    governed = _run_captured(
        tmp_path, "governed-large-total", ["/usr/bin/env"], child_env, True
    )

    assert governed[0].returncode == plain[0].returncode == 0
    assert sorted(governed[1].splitlines()) == sorted(plain[1].splitlines())
    assert governed[2] == plain[2] == b""


def test_governed_stub_rejects_bad_environment_payload(tmp_path, monkeypatch):
    marker = tmp_path / "client-ran"
    monkeypatch.setattr(
        process_module, "_snapshot_environment", lambda _env: b"bad payload"
    )

    outcome = run_process(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=30,
        stdout_path=tmp_path / "bad-payload.out",
        stderr_path=tmp_path / "bad-payload.err",
        control=ProcessControl(
            on_start=lambda _pid, _pgid: None,
            hold_before_exec=True,
        ),
    )

    assert outcome.returncode == process_module.BARRIER_FAILURE_EXIT
    assert not marker.exists()
    assert (tmp_path / "bad-payload.err").read_bytes() == b""


def test_governed_venv_interpreter_matches_plain_popen(tmp_path):
    code = (
        "import json, os, sys; "
        "print(json.dumps({'prefix': sys.prefix, 'executable': sys.executable, "
        "'environment': dict(os.environ)}, sort_keys=True))"
    )
    child_env = dict(os.environ)
    child_env.pop("LC_ALL", None)
    child_env["LC_CTYPE"] = "C"

    governed = _run_captured(
        tmp_path, "governed-venv", [sys.executable, "-c", code], child_env, True
    )
    plain = _run_captured(
        tmp_path, "plain-venv", [sys.executable, "-c", code], child_env, False
    )

    assert governed[0].returncode == plain[0].returncode == 0
    assert json.loads(governed[1]) == json.loads(plain[1])
    assert governed[2] == plain[2] == b""


def test_governed_stub_restores_subprocess_signal_dispositions(tmp_path):
    command = '(yes; echo "yes rc=$?" >&2) | head -1 >/dev/null'
    child_env = {"PATH": "/usr/bin:/bin"}

    governed = _run_captured(
        tmp_path, "governed-signals", ["/bin/sh", "-c", command], child_env, True
    )
    plain = _run_captured(
        tmp_path, "plain-signals", ["/bin/sh", "-c", command], child_env, False
    )

    assert governed[0].returncode == plain[0].returncode == 0
    assert governed[2] == plain[2]
    assert b"yes rc=141" in governed[2]


def test_snapshot_failure_matches_popen_without_leaking_pipe_fds(tmp_path):
    invalid_env = {object(): "value"}
    with pytest.raises(Exception) as plain:
        subprocess.Popen(["/usr/bin/true"], env=invalid_env)
    before = _open_fds()

    with pytest.raises(type(plain.value)):
        run_process(
            ["/usr/bin/true"],
            cwd=tmp_path,
            env=invalid_env,
            timeout_seconds=30,
            stdout_path=tmp_path / "invalid-env.out",
            stderr_path=tmp_path / "invalid-env.err",
            control=ProcessControl(
                on_start=lambda _pid, _pgid: None,
                hold_before_exec=True,
            ),
        )

    assert _open_fds() == before


@pytest.mark.parametrize("remove_before_release, expected", [(False, 126), (True, 127)])
def test_governed_stub_reports_exec_failure(
    tmp_path, remove_before_release, expected
):
    client = tmp_path / "client"
    client.write_text(
        "#!/definitely/missing/interpreter\n" if remove_before_release else "not executable",
        encoding="utf-8",
    )
    client.chmod(0o755)

    def on_start(_pid, _pgid):
        if remove_before_release:
            client.unlink()

    outcome = run_process(
        [str(client)],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout_seconds=30,
        stdout_path=tmp_path / "exec.out",
        stderr_path=tmp_path / "exec.err",
        control=ProcessControl(on_start=on_start, hold_before_exec=True),
    )

    lines = (tmp_path / "exec.err").read_text(encoding="utf-8").splitlines()
    assert outcome.returncode == expected
    assert lines == [lines[0]]
    assert lines[0].startswith("magnolia governed exec failed: ")


def test_parent_death_releases_barrier_without_running_client(tmp_path):
    parent = multiprocessing.get_context("spawn").Process(
        target=_die_during_pre_exec,
        args=(str(tmp_path),),
    )
    parent.start()
    parent.join(5)
    assert parent.exitcode == -signal.SIGKILL
    child_pid = int((tmp_path / "child.pid").read_text(encoding="utf-8"))
    try:
        assert _wait_until_process_gone(child_pid)
        assert not (tmp_path / "client-ran").exists()
    finally:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_keyboard_interrupt_before_exec_reaps_process_group(tmp_path):
    child = {}

    def interrupt(pid, _pgid):
        child["pid"] = pid
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        try:
            run_process(
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                env=dict(os.environ),
                timeout_seconds=30,
                stdout_path=tmp_path / "out.log",
                stderr_path=tmp_path / "err.log",
                control=ProcessControl(on_start=interrupt, hold_before_exec=True),
            )
        finally:
            if child and not _wait_until_process_gone(child["pid"]):
                os.killpg(child["pid"], signal.SIGKILL)
    assert _wait_until_process_gone(child["pid"])


@pytest.mark.parametrize("failure", ["missing", "barrier", "broken_pipe"])
def test_governed_pre_exec_failure_is_not_accounted(
    tmp_path, monkeypatch, failure
):
    binary = tmp_path / "client"
    if failure != "missing":
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    if failure == "barrier":
        monkeypatch.setattr(process_module.os, "write", lambda _fd, _body: 0)
    elif failure == "broken_pipe":
        def fail_release(_fd, _body):
            raise BrokenPipeError(32, "Broken pipe")

        monkeypatch.setattr(process_module.os, "write", fail_release)
    usage = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
    )
    selected = ResolvedRoute(
        "opencode", "provider/model", "provider", "opencode:provider", "worker", "test"
    )
    executor = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
        governance=InvocationGovernance(usage, selected, "run-1", "project"),
    )

    with pytest.raises(WorkerError, match="cannot start worker") as governed:
        executor.run("work", tmp_path, tmp_path / "attempt")

    if failure == "missing":
        plain = OpenCodeExecutor(
            str(binary), "provider/model", "campaign-worker"
        )
        with pytest.raises(WorkerError, match="cannot start worker") as ungoverned:
            plain.run("work", tmp_path, tmp_path / "plain-attempt")
        assert str(governed.value) == str(ungoverned.value)

    assert usage.report(datetime.now(timezone.utc).date())["day"]["runs"] == 0
    state = json.loads(usage.state_path.read_text(encoding="utf-8"))
    assert state["reservations"] == {}


def test_real_epipe_keeps_start_error_and_cancels_reservation(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    launcher_pids = []

    class DiesBeforeRelease(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._die_on_poll = "-I" in self.args and "-S" in self.args
            if self._die_on_poll:
                launcher_pids.append(self.pid)

        def poll(self):
            if self._die_on_poll:
                self._die_on_poll = False
                os.kill(self.pid, signal.SIGKILL)
                time.sleep(0.1)
                return None
            return super().poll()

    def zombie_group_eperm(_pid, _sent_signal):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(process_module.subprocess, "Popen", DiesBeforeRelease)
    monkeypatch.setattr(process_module, "_signal_group", zombie_group_eperm)
    binary = tmp_path / "client"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    usage = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
    )
    selected = ResolvedRoute(
        "opencode", "provider/model", "provider", "opencode:provider", "worker", "test"
    )
    executor = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
        governance=InvocationGovernance(usage, selected, "run-1", "project"),
    )

    try:
        with pytest.raises(WorkerError, match="Broken pipe"):
            executor.run("work", tmp_path, tmp_path / "attempt")
    finally:
        for pid in launcher_pids:
            _reap_test_process(pid)

    assert not (tmp_path / "usage-ledger.jsonl").exists()
    state = json.loads(usage.state_path.read_text(encoding="utf-8"))
    assert state["reservations"] == {}
    assert not (tmp_path / "client-ran").exists()


def test_stub_killed_mid_environment_read_is_start_error(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    real_write = process_module.os.write
    launchers = []

    class RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if "-I" in self.args and "-S" in self.args:
                launchers.append(self)

    def kill_after_payload_prefix(fd, body):
        if not launchers or len(body) <= 1024:
            return real_write(fd, body)
        written = real_write(fd, body[:1024])
        os.kill(launchers[-1].pid, signal.SIGKILL)
        launchers[-1].wait(timeout=5)
        return written

    monkeypatch.setattr(process_module.subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr(process_module.os, "write", kill_after_payload_prefix)
    binary = tmp_path / "client"
    marker = tmp_path / "client-ran"
    binary.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    binary.chmod(0o755)
    usage = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
    )
    selected = ResolvedRoute(
        "opencode", "provider/model", "provider", "opencode:provider", "worker", "test"
    )
    executor = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
        environment={"BIG": "x" * (100 * 1024)},
        governance=InvocationGovernance(usage, selected, "run-1", "project"),
    )

    try:
        with pytest.raises(WorkerError, match="Broken pipe"):
            executor.run("work", tmp_path, tmp_path / "attempt")
    finally:
        for launcher in launchers:
            if launcher.returncode is None:
                launcher.kill()
                launcher.wait()

    assert not (tmp_path / "usage-ledger.jsonl").exists()
    state = json.loads(usage.state_path.read_text(encoding="utf-8"))
    assert state["reservations"] == {}
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["callback", "keyboard_interrupt"])
def test_dead_launcher_cleanup_preserves_on_start_failure(
    tmp_path, monkeypatch, failure
):
    launcher_pids = []

    def zombie_group_eperm(_pid, _sent_signal):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    def fail_after_launcher_exit(pid, _pgid):
        launcher_pids.append(pid)
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.1)
        if failure == "keyboard_interrupt":
            raise KeyboardInterrupt
        raise RuntimeError("caller callback failed")

    monkeypatch.setattr(process_module, "_signal_group", zombie_group_eperm)
    binary = tmp_path / "client"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    usage = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
    )
    selected = ResolvedRoute(
        "opencode", "provider/model", "provider", "opencode:provider", "worker", "test"
    )
    executor = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
        governance=InvocationGovernance(usage, selected, "run-1", "project"),
    )

    expected = KeyboardInterrupt if failure == "keyboard_interrupt" else WorkerError
    try:
        with pytest.raises(expected) as raised:
            executor.run(
                "work",
                tmp_path,
                tmp_path / "attempt",
                control=ProcessControl(on_start=fail_after_launcher_exit),
            )
    finally:
        for pid in launcher_pids:
            _reap_test_process(pid)

    if failure == "callback":
        assert "caller callback failed" in str(raised.value)
    assert not (tmp_path / "usage-ledger.jsonl").exists()
    state = json.loads(usage.state_path.read_text(encoding="utf-8"))
    assert state["reservations"] == {}


def test_kill_and_reap_does_not_signal_reaped_leader(monkeypatch):
    signals = []

    class ReapedProcess:
        pid = 4242
        returncode = 0

        @staticmethod
        def wait(timeout=None):
            return 0

    monkeypatch.setattr(
        process_module,
        "_signal_group",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    process_module._kill_and_reap(ReapedProcess())

    assert signals == []


def test_kill_and_reap_swallows_eperm_only_after_leader_is_reaped(monkeypatch):
    class ReapedAfterSignal:
        pid = 4242
        returncode = None

        def __init__(self):
            self.poll_calls = 0
            self.wait_calls = 0

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls == 2:
                self.returncode = -signal.SIGKILL
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls += 1
            return self.returncode

    process = ReapedAfterSignal()

    def raise_eperm(_pid, _signal):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(
        process_module,
        "_signal_group",
        raise_eperm,
    )

    process_module._kill_and_reap(process)

    assert process.poll_calls == 2
    assert process.wait_calls == 1


def test_kill_and_reap_reraises_eperm_while_leader_is_live(monkeypatch):
    class LiveProcess:
        pid = 4242
        returncode = None

        def __init__(self):
            self.poll_calls = 0

        def poll(self):
            self.poll_calls += 1
            return None

        @staticmethod
        def wait(timeout=None):
            raise AssertionError("live leader must not be reaped after EPERM")

    process = LiveProcess()

    def raise_eperm(_pid, _signal):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(
        process_module,
        "_signal_group",
        raise_eperm,
    )

    with pytest.raises(PermissionError):
        process_module._kill_and_reap(process)

    assert process.poll_calls == 2


def test_resume_and_repair_each_reserve_and_record(tmp_path):
    binary = tmp_path / "client"
    binary.write_text(
        """#!/usr/bin/env python3
import json, sys
resumed = '--session' in sys.argv
session = 'same-session'
print(json.dumps({'sessionID': session, 'part': {'type': 'step-start'}}))
if resumed:
    print(json.dumps({'sessionID': session, 'part': {'type': 'step-finish', 'tokens': {'input': 30, 'output': 10}}}))
    print(json.dumps({'sessionID': session, 'part': {'type': 'text', 'messageID': 'm2', 'text': 'done'}}))
else:
    print(json.dumps({'sessionID': session, 'part': {'type': 'step-finish', 'tokens': {'input': 100, 'output': 20}}}))
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    clock = FakeClock(datetime(2026, 11, 15, 12, tzinfo=timezone.utc))

    usage = Governor(
        Policy(),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
        sleeper=clock.advance,
    )
    selected = ResolvedRoute(
        "opencode", "provider/model", "provider", "opencode:provider", "worker", "test"
    )
    executor = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
        governance=InvocationGovernance(
            governor=usage,
            route=selected,
            parent_run_id="run-1",
            project_digest="project",
            role_cap=60,
        ),
    )

    started_at = clock()
    result = executor.run("work", tmp_path, tmp_path / "attempt")

    rows = [json.loads(line) for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()]
    assert result.attempts == 2
    assert result.final_text == "done"
    assert len(rows) == 2
    assert rows[0]["usage"]["input_tokens"] == 100
    assert rows[1]["usage"]["input_tokens"] == 30
    assert result.usage["input_tokens"] == 130
    assert (clock() - started_at).total_seconds() >= 30
    assert usage.report(clock().date())["waits"] == []

    ungoverned = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
    ).run("work", tmp_path, tmp_path / "ungoverned-attempt")
    assert result.final_text == ungoverned.final_text
    assert result.usage == ungoverned.usage


class FakeClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
