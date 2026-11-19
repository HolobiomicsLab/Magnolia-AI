import os

from compchem_memory.harness import RENDERERS, Launch
from compchem_memory.harness import __main__ as launch_cli
from compchem_memory.harness.files import merged_json, merged_toml

from .test_launch_cli import FakeRenderer, _args, _root


def _run(tmp_path, monkeypatch, links_for):
    root = _root(tmp_path)

    class Renderer(FakeRenderer):
        def __call__(self, _neutral, context):
            return Launch(argv=["fake-client"], env={}, files={}, links=links_for(context))

    monkeypatch.setitem(RENDERERS, "claude", Renderer())
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")
    return root


def test_launch_creates_and_replaces_stale_skill_links(tmp_path, monkeypatch, capsys):
    target = tmp_path / "skills" / "alpha"
    other = tmp_path / "skills" / "beta"
    target.mkdir(parents=True)
    other.mkdir()
    root = _run(
        tmp_path, monkeypatch,
        lambda c: {c.project_dir / ".claude" / "skills" / "alpha": target},
    )
    link = root / "projects" / "demo" / ".claude" / "skills" / "alpha"
    link.parent.mkdir(parents=True)
    link.symlink_to(other)

    assert launch_cli.main(_args(root)) == 0
    assert os.readlink(link) == str(target)
    assert launch_cli.main(_args(root)) == 0
    assert os.readlink(link) == str(target)
    assert capsys.readouterr().err == ""


def test_launch_leaves_real_directory_and_warns(tmp_path, monkeypatch, capsys):
    target = tmp_path / "skills" / "alpha"
    target.mkdir(parents=True)
    root = _run(
        tmp_path, monkeypatch,
        lambda c: {c.project_dir / ".claude" / "skills" / "alpha": target},
    )
    real = root / "projects" / "demo" / ".claude" / "skills" / "alpha"
    real.mkdir(parents=True)
    (real / "mine.md").write_text("keep", encoding="utf-8")

    assert launch_cli.main(_args(root)) == 0
    assert not real.is_symlink()
    assert (real / "mine.md").read_text() == "keep"
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("warning: skill link skipped") and str(real) in err


def test_merged_helpers_match_the_writers(tmp_path):
    path = tmp_path / "x.json"
    content, conflicts = merged_json(path, lambda *_: False, {"a": 1})
    assert content == b'{\n  "a": 1\n}\n' and conflicts == [] and not path.exists()

    toml = tmp_path / "x.toml"
    toml.write_bytes(b"# c\nk = 1\n")
    content, conflicts, backup = merged_toml(toml, lambda *_: False, {"a": 1})
    assert backup == b"# c\nk = 1\n" and b"a = 1" in content
    assert toml.read_bytes() == b"# c\nk = 1\n"


def test_launch_refuses_links_through_a_skills_dir_that_leaves_the_project(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / "skills" / "alpha"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = _run(
        tmp_path, monkeypatch,
        lambda c: {c.project_dir / ".claude" / "skills" / "alpha": target},
    )
    skills = root / "projects" / "demo" / ".claude" / "skills"
    skills.parent.mkdir(parents=True)
    skills.symlink_to(outside)

    assert launch_cli.main(_args(root)) == 0
    assert list(outside.iterdir()) == []
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.startswith("warning: skill link skipped")
