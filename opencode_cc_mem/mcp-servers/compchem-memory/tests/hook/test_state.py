"""Locked per-session state files."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from compchem_memory.hook.state import SessionState, safe_component


def test_state_roundtrip(tmp_path):
    with SessionState.open(tmp_path, "claude", "s1") as st:
        assert st.data == {}
        st.data["injections"] = 2
        st.data["entry_counts"] = {"Read": 1}

    with SessionState.open(tmp_path, "claude", "s1") as st:
        assert st.data == {"injections": 2, "entry_counts": {"Read": 1}}

    path = tmp_path / "hook-state" / "claude-s1.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "injections": 2,
        "entry_counts": {"Read": 1},
    }


def test_state_files_are_private_regardless_of_umask(tmp_path):
    previous_umask = os.umask(0o022)
    try:
        with SessionState.open(tmp_path, "claude", "s1") as state:
            state.data["injections"] = 1
    finally:
        os.umask(previous_umask)

    directory = tmp_path / "hook-state"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "claude-s1.json").stat().st_mode & 0o777 == 0o600
    assert (directory / "claude-s1.json.lock").stat().st_mode & 0o777 == 0o600


def test_state_sanitises_session_id(tmp_path):
    with SessionState.open(tmp_path, "claude", "a/b c:d") as st:
        st.data["injections"] = 1

    [path] = (tmp_path / "hook-state").glob("*.json")
    assert path.name == f"claude-{safe_component('a/b c:d')}.json"
    assert path.name.startswith("claude-a_b_c_d=")
    with SessionState.open(tmp_path, "claude", "a/b c:d") as st:
        assert st.data["injections"] == 1


@pytest.mark.parametrize("session_id", ["../../../../escape", "..", ".", "", "a/../../b", "x" * 500])
def test_state_dot_dot_session_id_stays_in_store(tmp_path, session_id):
    store = tmp_path / "store"
    with SessionState.open(store, "../claude", session_id) as st:
        st.data["n"] = 1

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all((store / "hook-state") in p.parents for p in written)
    assert all(len(p.name) < 255 for p in written)


def test_safe_component_is_distinct_and_keeps_plain_ids():
    assert safe_component("ses-1_A.b") == "ses-1_A.b"
    assert safe_component("01a1ded9-75f1-7e93-aec8-19119f3d6509") == "01a1ded9-75f1-7e93-aec8-19119f3d6509"
    names = {safe_component(v) for v in ("a/b", "a_b", "a:b", "..", ".", "", ".hidden")}
    assert len(names) == 7
    for name in names:
        assert name not in ("", ".", "..") and "/" not in name and not name.startswith(".")


def test_state_lock_not_held_by_slow_work(tmp_path):
    started = threading.Event()
    finished = threading.Event()

    def slow_worker():
        with SessionState.open(tmp_path, "claude", "s1") as st:
            st.data["injections"] = st.data.get("injections", 0) + 1
        started.set()
        time.sleep(3.0)
        with SessionState.open(tmp_path, "claude", "s1") as st:
            st.data["injections"] = st.data.get("injections", 0) + 1
        finished.set()

    thread = threading.Thread(target=slow_worker)
    thread.start()
    try:
        assert started.wait(2.0)
        begin = time.monotonic()
        with SessionState.open(tmp_path, "claude", "s1") as st:
            st.data["injections"] = st.data.get("injections", 0) + 1
        elapsed = time.monotonic() - begin
        assert elapsed < 0.5, f"open blocked for {elapsed:.2f}s behind slow work"
    finally:
        thread.join(10.0)

    assert finished.is_set()
    with SessionState.open(tmp_path, "claude", "s1") as st:
        assert st.data["injections"] == 3


def test_state_concurrent_updates_keep_all_counts(tmp_path):
    def bump():
        with SessionState.open(tmp_path, "claude", "s1") as st:
            st.data["injections"] = st.data.get("injections", 0) + 1

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda _: bump(), range(40)))

    with SessionState.open(tmp_path, "claude", "s1") as st:
        assert st.data["injections"] == 40


def test_state_corrupt_file_opens_empty_and_is_overwritten(tmp_path):
    path = tmp_path / "hook-state" / "claude-s1.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with SessionState.open(tmp_path, "claude", "s1") as st:
        assert st.data == {}
        st.data["injections"] = 1

    assert json.loads(path.read_text(encoding="utf-8")) == {"injections": 1}


def test_state_body_exception_saves_nothing(tmp_path):
    with SessionState.open(tmp_path, "claude", "s1") as st:
        st.data["injections"] = 1

    with pytest.raises(RuntimeError):
        with SessionState.open(tmp_path, "claude", "s1") as st:
            st.data["injections"] = 99
            raise RuntimeError("boom")

    with SessionState.open(tmp_path, "claude", "s1") as st:
        assert st.data["injections"] == 1
