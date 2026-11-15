import json
from pathlib import Path

from compchem_memory.harness.neutral import load


ROOT = Path(__file__).parents[4]
TEMPLATE = ROOT / "opencode.json.template"


def _subs(project: str = "demo") -> dict[str, str]:
    return {
        "@@PROJECT@@": project,
        "@@MAGNOLIA_ROOT@@": str(ROOT.parent),
        "@@PYTHON@@": "/venv/bin/python",
        "@@COMPCHEM_TOOLS_PORT@@": "8123",
    }


def test_load_reads_three_servers_and_disables_memory_without_a_model():
    config = load(TEMPLATE, _subs(), {"memory_enabled": True})

    assert [server.name for server in config.servers] == [
        "compchem-tools",
        "compchem-memory",
        "perspicacite",
    ]
    tools, memory, perspicacite = config.servers
    assert tools.kind == "remote"
    assert tools.url == "http://127.0.0.1:8123/mcp"
    assert memory.kind == "local"
    assert memory.command == ["/venv/bin/python", "-m", "compchem_memory.server"]
    assert memory.enabled is False
    assert memory.env["MAGNOLIA_PROJECT_DIR"].endswith("/projects/demo")
    assert "MAGNOLIA_MEMORY_MODEL" not in memory.env
    assert perspicacite.timeout_ms == 660000
    assert config.deny_tools == frozenset({"bash"})


def test_load_injects_memory_model_only_into_memory_environment():
    setup = {
        "memory_enabled": True,
        "memory_model": "memory-model",
        "memory_provider": "provider",
    }

    config = load(TEMPLATE, _subs(), setup)

    memory = config.servers[1]
    assert memory.enabled is True
    assert memory.env["MAGNOLIA_MEMORY_MODEL"] == "memory-model"
    assert memory.env["MAGNOLIA_MEMORY_PROVIDER"] == "provider"
    assert all("MAGNOLIA_MEMORY_MODEL" not in server.env for server in config.servers[::2])


def test_load_matches_render_config_placeholder_values():
    config = load(TEMPLATE, _subs("a b"), {})
    raw = json.loads(
        TEMPLATE.read_text(encoding="utf-8")
        .replace("@@MEMORY_ENABLED@@", "false")
    )

    assert len(config.servers) == len(raw["mcp"])
    assert config.servers[1].env["MAGNOLIA_ROOT"] == str(ROOT.parent)


def test_load_json_escapes_awkward_substitutions_in_real_template():
    project = 'a "b" é c'
    python = r"C:\Magnolia\venv\python.exe"

    config = load(TEMPLATE, _subs(project) | {"@@PYTHON@@": python}, {})

    memory = config.servers[1]
    assert memory.command[0] == python
    assert memory.env["MAGNOLIA_PROJECT_DIR"].endswith(f"/projects/{project}")


def test_substitute_handles_bare_numeric_placeholder(tmp_path):
    template = tmp_path / "template.json"
    template.write_text('{"port": @@COMPCHEM_TOOLS_PORT@@, "enabled": @@MEMORY_ENABLED@@}')

    assert load(template, {"@@COMPCHEM_TOOLS_PORT@@": "8123"}, {}) is not None
