import json
import os
import tomllib
from datetime import date, datetime, time, timezone
from pathlib import Path

from compchem_memory.harness import files


MAGNOLIA_SERVERS = {"compchem-memory", "compchem-tools", "perspicacite"}


def _owned(kind, value):
    if kind == "server":
        return value.get("name") in MAGNOLIA_SERVERS
    if isinstance(value, dict):
        command = value.get("command", "")
        if isinstance(command, str) and "-m compchem_memory.hook" in command:
            return True
    return False


def _owned_entry(kind, name, entry):
    if kind == "server":
        return name in MAGNOLIA_SERVERS
    return _owned(kind, entry)


def _keep_all(_kind, _name, _entry):
    return False


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_render_preserves_member_entries_and_is_idempotent(tmp_path, monkeypatch):
    json_path = tmp_path / "hooks.json"
    member_hook = {"command": "member-hook", "timeout": 4}
    magnolia_hook = {
        "name": "magnolia-prompt",
        "command": "/python -m compchem_memory.hook codex prompt",
    }
    _write_json(json_path, {"hooks": [member_hook], "member": True})
    replaces = []
    real_replace = files.os.replace

    def record_replace(source, destination):
        replaces.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(files.os, "replace", record_replace)
    files.merge_json(json_path, _owned_entry, {"hooks": [magnolia_hook]})
    first = json_path.read_bytes()
    files.merge_json(json_path, _owned_entry, {"hooks": [magnolia_hook]})

    assert json_path.read_bytes() == first
    assert json.loads(first) == {"hooks": [member_hook, magnolia_hook], "member": True}
    assert len(replaces) == 2

    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        'model = "member-model"\n'
        '[mcp_servers.member]\ncommand = ["member-server"]\n'
        '[mcp_servers.compchem-memory]\ncommand = ["old"]\n',
        encoding="utf-8",
    )
    magnolia = {
        "mcp_servers": {
            "compchem-memory": {"command": ["/python", "-m", "compchem_memory.server"]},
            "compchem-tools": {"url": "http://127.0.0.1:8123/mcp"},
        }
    }
    files.merge_toml(toml_path, _owned_entry, magnolia)
    first_toml = toml_path.read_bytes()
    files.merge_toml(toml_path, _owned_entry, magnolia)

    assert toml_path.read_bytes() == first_toml
    parsed = tomllib.loads(first_toml.decode("utf-8"))
    assert parsed["model"] == "member-model"
    assert parsed["mcp_servers"]["member"] == {"command": ["member-server"]}
    assert parsed["mcp_servers"]["compchem-memory"]["command"][-1] == "compchem_memory.server"


def test_render_quotes_awkward_paths(tmp_path):
    project = 'a "b" é c'
    command = [f"C:\\Magnolia\\{project}\\venv\\bin\\python", "-m", "compchem_memory.hook"]
    path = tmp_path / "config.toml"
    path.write_text(
        '[[hooks]]\nname = "member"\ncommand = "keep"\n'
        '[[hooks]]\nname = "magnolia-old"\ncommand = "old -m compchem_memory.hook codex stop"\n',
        encoding="utf-8",
    )

    files.merge_toml(
        path,
        _owned_entry,
        {"project": project, "hooks": [{"name": "magnolia-stop", "command": command}]},
    )

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["project"] == project
    assert parsed["hooks"] == [
        {"name": "member", "command": "keep"},
        {"name": "magnolia-stop", "command": command},
    ]


def test_json_writer_round_trips_awkward_unicode_and_controls(tmp_path):
    path = tmp_path / "settings.json"
    value = {"project": 'a "b" é c \\ root\nnext', "enabled": True, "count": 3}

    files.merge_json(path, lambda _kind, _name, _entry: True, value)

    assert json.loads(path.read_text(encoding="utf-8")) == value


def test_toml_preserves_stable_order_and_escapes_control_characters(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'first = "keep"\n'
        '[mcp_servers.member]\ncommand = ["before"]\n'
        '[mcp_servers.compchem-memory]\ncommand = ["old"]\n'
        '[member_tail]\nenabled = true\n',
        encoding="utf-8",
    )
    command = "line\x01\n\"\\"

    files.merge_toml(
        path,
        lambda _kind, name, _entry: name == "compchem-memory",
        {"mcp_servers": {"compchem-memory": {"command": [command], "count": 2}}},
    )
    first = path.read_bytes()
    files.merge_toml(
        path,
        lambda _kind, name, _entry: name == "compchem-memory",
        {"mcp_servers": {"compchem-memory": {"command": [command], "count": 2}}},
    )

    assert path.read_bytes() == first
    assert first.index(b"[mcp_servers.member]") < first.index(b"[mcp_servers.compchem-memory]")
    parsed = tomllib.loads(first.decode("utf-8"))
    assert parsed["mcp_servers"]["compchem-memory"] == {
        "command": [command], "count": 2
    }
    assert parsed["member_tail"] == {"enabled": True}


def test_json_preserves_member_mcp_server_fields(tmp_path):
    path = tmp_path / "settings.json"
    _write_json(
        path,
        {
            "mcp": {
                "member": {"command": ["member"], "timeout": 2},
                "compchem-memory": {
                    "command": ["old"],
                    "member_field": "preserve",
                },
            }
        },
    )

    files.merge_json(
        path,
        lambda _kind, name, _entry: name == "compchem-memory",
        {"mcp": {"compchem-memory": {"command": ["new"], "enabled": True}}},
    )

    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["mcp"]["member"] == {"command": ["member"], "timeout": 2}
    assert parsed["mcp"]["compchem-memory"] == {
        "command": ["new"],
        "enabled": True,
        "member_field": "preserve",
    }


def test_toml_round_trips_backslash_slash_without_corruption(tmp_path):
    path = tmp_path / "config.toml"
    value = {"member": r"a\/b"}

    files.merge_toml(path, _keep_all, value)
    first = path.read_bytes()
    files.merge_toml(path, _keep_all, value)

    assert path.read_bytes() == first
    assert tomllib.loads(first.decode()) == value


def test_toml_writer_supports_ordinary_member_values_and_is_idempotent(tmp_path):
    path = tmp_path / "config.toml"
    value = {
        "floats": [1.25, float("inf"), float("-inf"), float("nan")],
        "truth": True,
        "count": 4,
        "nested": [["member", 2], [False, 3.5]],
        "inline": [{"name": "one", "enabled": True}, {"name": "two", "values": [1, 2]}],
        "hooks": [
            {
                "name": "member",
                "command": "keep",
                "options": {
                    "retries": 2,
                    "when": datetime(2026, 11, 15, 12, 30, tzinfo=timezone.utc),
                },
            }
        ],
        "dates": [date(2026, 11, 15), time(12, 30, 1)],
    }
    path.write_text(files._toml_bytes(value).decode(), encoding="utf-8")
    original = tomllib.loads(path.read_text(encoding="utf-8"))

    files.merge_toml(path, _keep_all, {})
    first = path.read_bytes()
    files.merge_toml(path, _keep_all, {})

    parsed = tomllib.loads(first.decode())
    assert parsed["floats"][:2] == [1.25, float("inf")]
    assert parsed["floats"][2] == float("-inf")
    assert str(parsed["floats"][3]) == "nan"
    assert parsed["nested"] == value["nested"]
    assert parsed["inline"] == value["inline"]
    assert parsed["hooks"] == original["hooks"]
    assert parsed["hooks"][0]["options"]["when"] == datetime(
        2026, 11, 15, 12, 30, tzinfo=timezone.utc
    )
    assert parsed["dates"] == value["dates"]
    assert path.read_bytes() == first


def test_toml_quotes_non_ascii_bare_keys_and_escapes_delete(tmp_path):
    path = tmp_path / "config.toml"
    value = {"é": "delete:\x7f"}

    files.merge_toml(path, _keep_all, value)

    raw = path.read_text(encoding="utf-8")
    assert raw == '"é" = "delete:\\u007F"\n'
    assert tomllib.loads(raw) == value


def test_owned_entries_are_removed_but_member_entries_survive(tmp_path):
    json_path = tmp_path / "hooks.json"
    _write_json(
        json_path,
        {
            "mcp": {
                "compchem-memory": {"command": ["old"]},
                "member": {"command": ["keep"]},
            },
            "hooks": [
                {"name": "ordinary-magnolia-looking-name", "command": "keep"},
                {"name": "renamed", "command": "old -m compchem_memory.hook codex stop"},
            ],
        },
    )

    files.merge_json(json_path, _owned_entry, {"mcp": {}, "hooks": []})

    parsed = json.loads(json_path.read_text())
    assert parsed == {
        "mcp": {"member": {"command": ["keep"]}},
        "hooks": [{"name": "ordinary-magnolia-looking-name", "command": "keep"}],
    }


def test_atomic_write_preserves_existing_mode_and_symlink_target(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o640)
    link = tmp_path / "link.json"
    link.symlink_to(target)

    files.atomic_write(link, b'{"written": true}\n')

    assert link.is_symlink()
    assert json.loads(target.read_text()) == {"written": True}
    assert os.stat(target).st_mode & 0o777 == 0o640


def test_claude_event_hook_lists_receive_item_names(tmp_path):
    path = tmp_path / "settings.json"
    _write_json(
        path,
        {"hooks": {"PreToolUse": [
            {"name": "old", "command": "old -m compchem_memory.hook claude stop"},
            {"name": "member", "command": "keep"},
        ]}},
    )

    files.merge_json(path, _owned_entry, {"hooks": {"PreToolUse": []}})

    assert json.loads(path.read_text()) == {
        "hooks": {"PreToolUse": [{"name": "member", "command": "keep"}]}
    }


def test_owned_list_replacement_stays_at_the_original_position(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[hooks]]\nname = "before"\ncommand = "keep"\n'
        '[[hooks]]\nname = "old"\ncommand = "old -m compchem_memory.hook codex stop"\n'
        '[[hooks]]\nname = "after"\ncommand = "keep"\n',
        encoding="utf-8",
    )

    files.merge_toml(
        path,
        _owned_entry,
        {"hooks": [{"name": "new", "command": "new -m compchem_memory.hook codex stop"}]},
    )
    first = path.read_bytes()

    parsed = tomllib.loads(path.read_text())
    assert [item["name"] for item in parsed["hooks"]] == ["before", "new", "after"]
    files.merge_toml(
        path,
        _owned_entry,
        {"hooks": [{"name": "new", "command": "new -m compchem_memory.hook codex stop"}]},
    )
    assert path.read_bytes() == first


def test_toml_escaped_quote_before_hash_is_not_a_comment(tmp_path):
    path = tmp_path / "config.toml"
    original = 'basic = "escaped quote: \\""\n'
    path.write_text(original, encoding="utf-8")

    files.merge_toml(path, _keep_all, {"added": 1})

    assert not path.with_name("config.toml.magnolia-backup").exists()


def test_toml_comment_backup_is_created_once_and_never_overwritten(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# member explanation\nmember = "first"\n', encoding="utf-8")

    files.merge_toml(path, _keep_all, {"added": 1})
    backup = path.with_name("config.toml.magnolia-backup")
    assert backup.read_text() == '# member explanation\nmember = "first"\n'

    backup.write_text("keep original backup\n", encoding="utf-8")
    path.write_text('# changed comment\nmember = "second"\n', encoding="utf-8")
    files.merge_toml(path, _keep_all, {"added": 2})

    assert backup.read_text() == "keep original backup\n"


def test_toml_trailing_comment_is_backed_up_only_when_bytes_change(tmp_path):
    path = tmp_path / "config.toml"
    original = 'member = "first" # trailing explanation\n'
    path.write_text(original, encoding="utf-8")
    backup = path.with_name("config.toml.magnolia-backup")

    files.merge_toml(path, _keep_all, {"added": 1})
    assert backup.read_text() == original
    backup.write_text("original backup\n", encoding="utf-8")
    files.merge_toml(path, _keep_all, {"added": 2})
    assert backup.read_text() == "original backup\n"


def test_toml_hash_inside_strings_is_not_a_comment(tmp_path):
    path = tmp_path / "config.toml"
    original = (
        'basic = "# not a comment"\n'
        "literal = '# also not a comment'\n"
        'multiline = """first\n# still a string\nlast"""\n'
        "multiline_literal = '''first\n# still literal\nlast'''\n"
    )
    path.write_text(original, encoding="utf-8")

    files.merge_toml(path, _keep_all, {"added": 1})

    assert not path.with_name("config.toml.magnolia-backup").exists()


def _hook_group(command):
    return {"matcher": "*", "hooks": [{"type": "command", "command": command}]}


def _owned_group(kind, name, entry):
    if kind == "server":
        return name in MAGNOLIA_SERVERS
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    commands = [h.get("command", "") for h in hooks or [] if isinstance(h, dict)]
    return any("-m compchem_memory.hook" in c for c in commands) or _owned(kind, entry)


def test_nameless_member_hook_group_does_not_block_magnolia_group(tmp_path):
    path = tmp_path / "hooks.json"
    member = _hook_group("member-hook")
    old = _hook_group("/py -m compchem_memory.hook codex post-tool --old")
    path.write_text(json.dumps({"hooks": {"PostToolUse": [member, old]}}))
    new = _hook_group("/py -m compchem_memory.hook codex post-tool")

    conflicts = files.merge_json(path, _owned_group, {"hooks": {"PostToolUse": [new]}})
    first = path.read_bytes()
    files.merge_json(path, _owned_group, {"hooks": {"PostToolUse": [new]}})

    assert conflicts == []
    assert json.loads(first)["hooks"]["PostToolUse"] == [member, new]
    assert path.read_bytes() == first


def test_claude_mcp_servers_owned_by_name(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {
        "mine": {"command": "my-server"},
        "perspicacite": {"type": "http", "url": "http://old"},
    }}))
    magnolia = {"mcpServers": {"compchem-tools": {"type": "http", "url": "http://127.0.0.1:1/mcp"}}}

    files.merge_json(path, _owned_entry, magnolia)

    servers = json.loads(path.read_text())["mcpServers"]
    assert servers == {"mine": {"command": "my-server"},
                       "compchem-tools": {"type": "http", "url": "http://127.0.0.1:1/mcp"}}


def test_vibe_server_list_items_are_kind_server(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[mcp_servers]]\nname = "mine"\ntransport = "stdio"\ncommand = "x"\n')
    seen = []

    def owned(kind, name, entry):
        seen.append((kind, name))
        return kind == "server" and name in MAGNOLIA_SERVERS

    files.merge_toml(path, owned, {"mcp_servers": [{"name": "compchem-tools", "transport": "http", "url": "http://u"}]})

    data = tomllib.loads(path.read_text())
    assert ("server", "mine") in seen
    assert [s["name"] for s in data["mcp_servers"]] == ["mine", "compchem-tools"]


def test_merge_returns_named_conflicts(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.compchem-tools]\nurl = "http://member"\n')

    conflicts = files.merge_toml(path, _keep_all, {"mcp_servers": {"compchem-tools": {"url": "http://magnolia"}}})

    assert len(conflicts) == 1 and "compchem-tools" in conflicts[0]
    assert tomllib.loads(path.read_text())["mcp_servers"]["compchem-tools"]["url"] == "http://member"
