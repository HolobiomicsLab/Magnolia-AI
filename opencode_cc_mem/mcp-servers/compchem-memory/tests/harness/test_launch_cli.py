import json
import shutil
import subprocess
from pathlib import Path

import pytest

from compchem_memory.harness import Launch, RENDERERS
from compchem_memory.harness import __main__ as launch_cli


ROOT = Path(__file__).parents[4]
BASH_FRAMING_READER = r'''
while IFS= read -r -d '' a; do
    [ -z "$a" ] && break
    escaped=${a//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    escaped=${escaped//$'\n'/\\n}
    printf '{"argv":"%s"}\n' "$escaped"
done
while IFS= read -r -d '' a; do
    escaped=${a//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    escaped=${escaped//$'\n'/\\n}
    printf '{"env":"%s"}\n' "$escaped"
done
'''


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo" / "opencode_cc_mem"
    root.mkdir(parents=True)
    shutil.copy(ROOT / "opencode.json.template", root / "opencode.json.template")
    return root


class FakeRenderer:
    binary = "fake-client"

    def __init__(self):
        self.neutral = None
        self.context = None

    def __call__(self, neutral, context):
        self.neutral = neutral
        self.context = context
        config = context.project_dir / ".magnolia" / "harness" / "fake.json"
        return Launch(
            argv=[self.binary, "--project", context.project],
            env={"MAGNOLIA_PROJECT_DIR": str(context.project_dir), "EMPTY": ""},
            files={config: b'{"project": "demo"}\n'},
        )


def _args(root: Path) -> list[str]:
    return [
        "launch", "--harness", "claude", "--root", str(root),
        "--project", "demo", "--python", "/venv/bin/python", "--tools-port", "8123",
    ]


def test_launch_writes_files_and_emits_nul_framing(tmp_path, monkeypatch, capsysbinary):
    root = _root(tmp_path)
    renderer = FakeRenderer()
    monkeypatch.setitem(RENDERERS, "claude", renderer)
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")

    exit_code = launch_cli.main(_args(root))
    captured = capsysbinary.readouterr()

    assert exit_code == 0
    assert captured.err == b""
    config = root / "projects" / "demo" / ".magnolia" / "harness" / "fake.json"
    assert json.loads(config.read_text(encoding="utf-8")) == {"project": "demo"}
    assert renderer.context.root == root
    assert renderer.context.python == "/venv/bin/python"
    assert renderer.context.tools_port == 8123
    assert renderer.neutral.servers[0].url.endswith(":8123/mcp")

    proc = subprocess.run(
        ["/bin/bash", "-c", BASH_FRAMING_READER],
        input=captured.out,
        capture_output=True,
        check=True,
    )
    rows = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
    assert rows == [
        {"argv": "fake-client"},
        {"argv": "--project"},
        {"argv": "demo"},
        {"env": f"MAGNOLIA_PROJECT_DIR={config.parents[2]}"},
        {"env": "EMPTY="},
    ]


def test_launch_loads_setup_and_model(tmp_path, monkeypatch, capsysbinary):
    root = _root(tmp_path)
    setup_dir = root / ".magnolia"
    setup_dir.mkdir()
    (setup_dir / "llm-setup.json").write_text(
        json.dumps({"memory_enabled": True, "memory_model": "memory", "main_model": "deepseek/main",
                    "models": {"codex": "gpt-codex", "claude": "opus"}}),
        encoding="utf-8",
    )
    renderer = FakeRenderer()
    monkeypatch.setitem(RENDERERS, "codex", renderer)
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")
    args = _args(root)
    args[args.index("claude")] = "codex"

    assert launch_cli.main(args) == 0
    capsysbinary.readouterr()

    # The OpenCode main_model never reaches another client; models.<harness> does.
    assert renderer.context.model == "gpt-codex"
    assert renderer.neutral.servers[1].enabled is True
    assert renderer.neutral.servers[1].env["MAGNOLIA_MEMORY_MODEL"] == "memory"


def test_launch_missing_binary_exits_two_with_one_stderr_line(tmp_path, monkeypatch, capsysbinary):
    root = _root(tmp_path)
    monkeypatch.setitem(RENDERERS, "vibe", FakeRenderer())
    monkeypatch.setattr(launch_cli.shutil, "which", lambda _binary: None)

    args = _args(root)
    args[args.index("claude")] = "vibe"
    exit_code = launch_cli.main(args)
    captured = capsysbinary.readouterr()

    assert exit_code == 2
    assert captured.out == b""
    assert captured.err == b"missing client binary: fake-client\n"
    assert not (root / "projects").exists()


def test_launch_other_failure_exits_one_with_one_stderr_line(
    tmp_path, monkeypatch, capsysbinary
):
    monkeypatch.delitem(RENDERERS, "claude")
    exit_code = launch_cli.main(_args(_root(tmp_path)))
    captured = capsysbinary.readouterr()

    assert exit_code == 1
    assert captured.out == b""
    assert captured.err == b"no renderer registered for harness: claude\n"


def test_launch_renderer_failure_writes_no_files(tmp_path, monkeypatch, capsysbinary):
    root = _root(tmp_path)
    monkeypatch.setitem(RENDERERS, "claude", FakeRenderer())
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")

    monkeypatch.setattr(
        FakeRenderer,
        "__call__",
        lambda _self, _neutral, _context: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    assert launch_cli.main(_args(root)) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert captured.err == b"render failed\n"
    assert not (root / "projects").exists()


def test_launch_subprocess_emits_machine_readable_error(tmp_path):
    proc = subprocess.run(
        [
            str(Path(__import__("sys").executable)),
            "-c",
            "import sys; from compchem_memory.harness import RENDERERS, __main__ as cli;"
            " RENDERERS.clear(); raise SystemExit(cli.main(sys.argv[1:]))",
            *_args(_root(tmp_path)),
        ],
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 1
    assert proc.stdout == b""
    assert proc.stderr == b"no renderer registered for harness: claude\n"


def test_launch_help_accepts_all_named_clients():
    with pytest.raises(SystemExit) as exit_info:
        launch_cli.main(["launch", "--help"])

    assert exit_info.value.code == 0


def test_launch_escapes_real_template_substitutions(tmp_path, monkeypatch, capsysbinary):
    root = _root(tmp_path)
    renderer = FakeRenderer()
    monkeypatch.setitem(RENDERERS, "claude", renderer)
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")
    args = _args(root)
    args[args.index("demo")] = 'a "b" é c'
    args[args.index("/venv/bin/python")] = r"C:\Magnolia\venv\python.exe"

    assert launch_cli.main(args) == 0
    capsysbinary.readouterr()

    assert renderer.neutral.servers[1].command[0] == r"C:\Magnolia\venv\python.exe"
    assert renderer.neutral.servers[1].env["MAGNOLIA_PROJECT_DIR"].endswith(
        '/projects/a "b" é c'
    )


@pytest.mark.parametrize("escape", ["file", "parent", "backup"])
def test_launch_rejects_project_paths_resolving_outside_before_any_write(
    tmp_path, monkeypatch, capsysbinary, escape,
):
    from compchem_memory.harness.render_codex import render as render_codex

    root = _root(tmp_path)
    project = root / "projects" / "demo"
    external = tmp_path / "external"
    external.mkdir()
    original = b'{"hooks": {}}\n'
    (external / "hooks.json").write_bytes(original)
    project.mkdir(parents=True)
    if escape == "parent":
        (project / ".codex").symlink_to(external, target_is_directory=True)
    else:
        folder = project / ".codex"
        folder.mkdir()
        if escape == "file":
            (folder / "hooks.json").symlink_to(external / "hooks.json")
        else:
            (folder / "config.toml").write_text(
                '# member note\n[mcp_servers.compchem-tools]\ncommand = "old"\n',
                encoding="utf-8",
            )
            (folder / "config.toml.magnolia-backup").symlink_to(
                external / "escaped-backup.toml"
            )
    monkeypatch.setitem(RENDERERS, "codex", render_codex)
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")
    args = _args(root)
    args[args.index("claude")] = "codex"

    assert launch_cli.main(args) == 1
    captured = capsysbinary.readouterr()

    assert captured.out == b""
    assert captured.err.count(b"\n") == 1
    assert b"leaves the project" in captured.err
    assert (external / "hooks.json").read_bytes() == original
    assert not (external / "escaped-backup.toml").exists()
    assert not (project / ".magnolia").exists()


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        (["client", ""], {}, "argv items must not be empty"),
        (["client", "bad\0item"], {}, "argv items must not contain NUL"),
        (["client"], {"BAD\0KEY": "value"}, "env keys and values must not contain NUL"),
        (["client"], {"KEY": "bad\0value"}, "env keys and values must not contain NUL"),
        (["client"], {"": "value"}, "env keys must not be empty"),
        (["client"], {"BAD=KEY": "value"}, "env keys must not contain ="),
    ],
)
def test_launch_rejects_invalid_framing_before_writing_files(
    tmp_path, monkeypatch, capsysbinary, argv, env, message
):
    root = _root(tmp_path)
    class Renderer(FakeRenderer):
        def __call__(self, _neutral, context):
            return Launch(
                argv=argv,
                env=env,
                files={context.project_dir / "must-not-exist": b"written"},
            )

    monkeypatch.setitem(RENDERERS, "claude", Renderer())
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")

    assert launch_cli.main(_args(root)) == 1
    captured = capsysbinary.readouterr()

    assert captured.out == b""
    assert captured.err == f"{message}\n".encode()
    assert not (root / "projects").exists()


def test_launch_framing_round_trips_shell_sensitive_items(tmp_path, monkeypatch, capsysbinary):
    root = _root(tmp_path)
    argv = ["client", "line one\nline two", "equals=inside", " spaces ", 'quote"and\\slash']
    env = {"MULTILINE": "first\nsecond=2", "QUOTED": ' "value" \\ tail '}
    class Renderer(FakeRenderer):
        def __call__(self, _neutral, context):
            return Launch(argv=argv, env=env, files={context.project_dir / "written": b"yes"})

    monkeypatch.setitem(RENDERERS, "claude", Renderer())
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")

    assert launch_cli.main(_args(root)) == 0
    captured = capsysbinary.readouterr()
    proc = subprocess.run(
        ["/bin/bash", "-c", BASH_FRAMING_READER],
        input=captured.out,
        capture_output=True,
        check=True,
    )

    rows = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    assert [row["argv"] for row in rows if "argv" in row] == argv
    assert [row["env"] for row in rows if "env" in row] == [
        f"{key}={value}" for key, value in env.items()
    ]
    assert (root / "projects" / "demo" / "written").read_bytes() == b"yes"


def test_argparse_error_is_one_stderr_line_and_exit_one(capsysbinary):
    assert launch_cli.main(["unknown"]) == 1
    captured = capsysbinary.readouterr()

    assert captured.out == b""
    assert captured.err.startswith(b"argument command: invalid choice: 'unknown'")
    assert captured.err.endswith(b"\n")
    assert captured.err.count(b"\n") == 1


def test_launch_prints_one_warning_per_merge_conflict_and_exits_zero(
    tmp_path, monkeypatch, capsysbinary
):
    root = _root(tmp_path)
    class Renderer(FakeRenderer):
        def __call__(self, _neutral, context):
            return Launch(
                argv=["fake-client"],
                env={},
                files={},
                conflicts=[
                    "configuration conflict: mcp.compchem-memory (member entry preserved)",
                    "configuration conflict: mcpServers.member (member entry preserved)",
                ],
            )

    monkeypatch.setitem(RENDERERS, "claude", Renderer())
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")

    assert launch_cli.main(_args(root)) == 0
    captured = capsysbinary.readouterr()
    assert captured.err == (
        b"warning: configuration conflict: mcp.compchem-memory (member entry preserved)\n"
        b"warning: configuration conflict: mcpServers.member (member entry preserved)\n"
    )


def test_write_failure_after_conflict_prints_only_the_error(
    tmp_path, monkeypatch, capsysbinary
):
    root = _root(tmp_path)
    class Renderer(FakeRenderer):
        def __call__(self, _neutral, context):
            return Launch(
                argv=["fake-client"], env={},
                files={context.project_dir / "broken": b"content"},
                conflicts=["configuration conflict: hooks.old (member entry preserved)"],
            )

    monkeypatch.setitem(RENDERERS, "claude", Renderer())
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(
        launch_cli, "atomic_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    assert launch_cli.main(_args(root)) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert captured.err == b"write failed\n"


def test_launch_without_client_model_passes_none(tmp_path, monkeypatch, capsysbinary):
    root = _root(tmp_path)
    (root / ".magnolia").mkdir()
    (root / ".magnolia" / "llm-setup.json").write_text(
        json.dumps({"main_model": "deepseek/main"}), encoding="utf-8",
    )
    renderer = FakeRenderer()
    monkeypatch.setitem(RENDERERS, "codex", renderer)
    monkeypatch.setattr(launch_cli.shutil, "which", lambda binary: f"/bin/{binary}")
    args = _args(root)
    args[args.index("claude")] = "codex"

    assert launch_cli.main(args) == 0
    capsysbinary.readouterr()

    assert renderer.context.model is None
