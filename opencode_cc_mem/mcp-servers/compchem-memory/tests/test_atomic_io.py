"""Tests for atomic_write_text: same-dir temp + os.replace."""
from pathlib import Path
import pytest
from compchem_memory.atomic_io import atomic_write_text


def test_writes_new_file(tmp_path):
    target = tmp_path / "a.yaml"
    atomic_write_text(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_overwrites_existing_file(tmp_path):
    target = tmp_path / "b.yaml"
    target.write_text("old\n")
    atomic_write_text(target, "new\n")
    assert target.read_text() == "new\n"


def test_temp_file_is_cleaned_up(tmp_path):
    target = tmp_path / "c.yaml"
    atomic_write_text(target, "x\n")
    # No leftover .tmp* files in the directory
    leftovers = [p for p in tmp_path.iterdir() if p.name != "c.yaml"]
    assert leftovers == [], f"leftovers: {leftovers}"


def test_temp_file_is_in_same_dir(tmp_path, monkeypatch):
    """The temp file must live next to the target so os.replace is atomic
    within one filesystem (cross-fs rename is NOT atomic)."""
    target = tmp_path / "d.yaml"
    seen_temp_dirs: list[Path] = []
    import compchem_memory.atomic_io as mod
    real_NamedTemp = mod.tempfile.NamedTemporaryFile

    def spy(*a, **kw):
        seen_temp_dirs.append(Path(kw.get("dir", ".")))
        return real_NamedTemp(*a, **kw)

    monkeypatch.setattr(mod.tempfile, "NamedTemporaryFile", spy)
    atomic_write_text(target, "ok\n")
    assert seen_temp_dirs and seen_temp_dirs[0] == tmp_path


def test_failure_does_not_corrupt_existing(tmp_path, monkeypatch):
    """If os.replace fails, the existing target must be unchanged."""
    target = tmp_path / "e.yaml"
    target.write_text("untouched\n")
    import compchem_memory.atomic_io as mod
    monkeypatch.setattr(mod.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated")))
    with pytest.raises(OSError):
        atomic_write_text(target, "would-be-new\n")
    assert target.read_text() == "untouched\n"
