"""Cross-language job-notice queue concurrency regression tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from compchem_memory.hook.notices import commit_notices, peek_notices, release_notices
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.job_notices import (
    JobNoticeLock,
    _notices_path,
    drain_job_notices,
    push_job_notice,
)


def _node_with_type_stripping() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the real TypeScript consumer")
    version = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
    major, minor = (int(part) for part in version.split(".")[:2])
    supported = major >= 24 or (major == 23 and minor >= 6) or (major == 22 and minor >= 18)
    if not supported:
        pytest.skip(f"Node {version} cannot strip TypeScript; need 22.18+, 23.6+, or 24+")
    return node


def test_typescript_and_python_consumers_never_lose_or_duplicate(tmp_path, monkeypatch):
    """A writer, real Node plugin, and hook consumer share one queue safely."""
    node = _node_with_type_stripping()
    acquisition_failures: list[str] = []
    original_acquire = JobNoticeLock.acquire

    def checked_acquire(self):
        acquired = original_acquire(self)
        if not acquired and self.wait is None:
            acquisition_failures.append(f"{self.path}: {self.last_error}")
        return acquired

    monkeypatch.setattr(JobNoticeLock, "acquire", checked_acquire)
    plugin = Path(__file__).resolve().parents[4] / ".opencode" / "plugins" / "magnolia-job-notify.ts"
    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    paths = ProjectPaths(tmp_path, project, store, "project", False)
    queue = _notices_path(str(project))
    node_output = tmp_path / "node-deliveries.txt"

    for index in range(25):
        push_job_notice(
            str(project), run_id=f"seed-{index}", tool="xtb", state="done", category="success"
        )

    driver = tmp_path / "notice-driver.ts"
    driver.write_text(
        f'import {{ appendFileSync }} from "node:fs";\n'
        f'import {{ startPolling }} from "{plugin.as_uri()}";\n'
        "const [queue, output, milliseconds] = process.argv.slice(2);\n"
        "const stop = startPolling(queue, (_title, message) => {\n"
        '  appendFileSync(output, message + "\\n");\n'
        "}, 5);\n"
        "setTimeout(stop, Number(milliseconds));\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [node, str(driver), str(queue), str(node_output), "4000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**dict(os.environ), "MAGNOLIA_PYTHON": sys.executable},
    )

    expected = {f"seed-{index}" for index in range(25)}
    errors: list[BaseException] = []
    python_delivered: list[str] = []
    stop_consumer = threading.Event()

    def writer(worker: int) -> None:
        try:
            for index in range(50):
                run_id = f"writer-{worker}-{index}"
                expected.add(run_id)
                push_job_notice(
                    str(project), run_id=run_id, tool="xtb", state="done", category="success"
                )
        except BaseException as error:  # pragma: no cover - reported in the assertion
            errors.append(error)

    def consumer() -> None:
        state = SimpleNamespace(data={})
        try:
            while not stop_consumer.is_set():
                notices = peek_notices(paths, state)
                if notices is None:
                    time.sleep(0.002)
                    continue
                try:
                    # Recording between peek and commit models client delivery.
                    python_delivered.extend(
                        json.loads(line)["run_id"] for line in notices.job_lines
                    )
                    commit_notices(paths, "json", "stress", notices)
                finally:
                    if notices.job_lock is not None:
                        release_notices(notices)
        except BaseException as error:  # pragma: no cover - reported in the assertion
            errors.append(error)

    consumer_thread = threading.Thread(target=consumer)
    writer_threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    consumer_thread.start()
    for thread in writer_threads:
        thread.start()

    deadline = time.monotonic() + 12
    while process.poll() is None and time.monotonic() < deadline:
        with JobNoticeLock(str(project), wait=0.1) as held:
            if held and queue.exists():
                try:
                    for line in queue.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            json.loads(line)
                except BaseException as error:  # pragma: no cover - assertion reports it
                    errors.append(error)
                    break
        time.sleep(0.005)
    if process.poll() is None:  # pragma: no cover - defensive timeout
        process.kill()
    stdout, stderr = process.communicate(timeout=5)
    for thread in writer_threads:
        thread.join(timeout=5)
    stop_consumer.set()
    consumer_thread.join(timeout=5)

    tail = [row["run_id"] for row in drain_job_notices(str(project), limit=1000)]
    node_delivered = []
    if node_output.exists():
        for message in node_output.read_text(encoding="utf-8").splitlines():
            match = re.search(r"run …(\S+)$", message)
            if match:
                node_delivered.append(match.group(1))

    assert process.returncode == 0, f"Node failed: {stdout}\n{stderr}"
    assert not errors, [repr(error) for error in errors]
    assert not acquisition_failures, acquisition_failures
    assert all(not thread.is_alive() for thread in (*writer_threads, consumer_thread))
    assert len(expected) == 225  # 25 seeds + 4 writers × 50 notices
    counts = Counter(python_delivered + node_delivered + tail)
    assert counts == Counter({run_id: 1 for run_id in expected}), {
        "missing": sorted(set(expected) - set(counts)),
        "duplicates": sorted(run_id for run_id, count in counts.items() if count > 1),
        "unexpected": sorted(set(counts) - expected),
        "node_stderr": stderr,
    }
    assert not queue.exists()


def test_typescript_streams_notice_batch_larger_than_spawn_buffer(tmp_path):
    """A valid multi-megabyte drain must not disappear behind ENOBUFS."""
    node = _node_with_type_stripping()
    plugin = Path(__file__).resolve().parents[4] / ".opencode" / "plugins" / "magnolia-job-notify.ts"
    project = tmp_path / "project"
    queue = _notices_path(str(project))
    queue.parent.mkdir(parents=True)
    output = tmp_path / "large-deliveries.txt"
    run_ids = [f"large-{index}-" + "x" * 200_000 + f"-tail-{index}" for index in range(10)]
    for run_id in run_ids:
        push_job_notice(str(project), run_id=run_id, tool="xtb", state="done", category="success")

    driver = tmp_path / "large-notice-driver.ts"
    driver.write_text(
        f'import {{ appendFileSync }} from "node:fs";\n'
        f'import {{ startPolling }} from "{plugin.as_uri()}";\n'
        "const [queue, output] = process.argv.slice(2);\n"
        "const stop = startPolling(queue, (_title, message) => {\n"
        '  appendFileSync(output, message + "\\n");\n'
        "}, 20);\n"
        "setTimeout(stop, 1500);\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [node, str(driver), str(queue), str(output)],
        capture_output=True,
        text=True,
        timeout=15,
        env={**dict(os.environ), "MAGNOLIA_PYTHON": sys.executable},
    )

    assert process.returncode == 0, process.stderr
    messages = output.read_text(encoding="utf-8").splitlines()
    delivered_tails = [re.search(r"run …(\S+)$", message).group(1) for message in messages]
    assert Counter(delivered_tails) == Counter({run_id[-24:]: 1 for run_id in run_ids})
    assert not queue.exists()
