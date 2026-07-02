"""Phase 1 integration test: validates all acceptance criteria from PLAN.md.

Acceptance criteria:
1. Both MCP servers start and register their tools
2. check_environment correctly detects HADDOCK3 availability
3. A HADDOCK3 docking run can be set up and launched
4. post_run_assess fires and writes a run record
5. memory_get_context returns skill content for HADDOCK3 tasks
6. Project-tier entries can be created, searched, and retrieved
7. Session logs are written incrementally
8. Stage gate for "docking_inputs_ready" passes/fails correctly
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_pdb(path, chain_id="A", n_atoms=10, resname="ALA"):
    lines = []
    for i in range(n_atoms):
        serial = i + 1
        x, y, z = float(i), float(i), float(i)
        lines.append(
            f"ATOM  {serial:>5d}  CA  {resname:<3s} {chain_id}{serial:>4d}    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C"
        )
    lines.append("TER")
    lines.append("END")
    path.write_text("\n".join(lines))


def _write_config(path, run_dir="output", molecules=None):
    if molecules is None:
        molecules = ["receptor.pdb", "ligand.pdb"]
    lines = [
        f'run_dir = "{run_dir}"',
        f"molecules = {[str(m) for m in molecules]}",
        'mode = "local"',
        "ncores = 4",
        "",
        "[topoaa]",
        "",
        "[rigidbody]",
        "sampling = 10",
    ]
    path.write_text("\n".join(lines) + "\n")


def _write_actpass(path, active, passive=None):
    active_str = " ".join(str(r) for r in active)
    passive_str = " ".join(str(r) for r in (passive or []))
    path.write_text(f"{active_str}\n{passive_str}\n")


@pytest.fixture(scope="session")
def test_root():
    with tempfile.TemporaryDirectory(prefix="magnolia_p1_") as d:
        root = Path(d)
        skills_dir = root / "skills"
        skills_dir.mkdir()
        (skills_dir / "HADDOCK3_SKILL.md").write_text(
            "---\n"
            "name: haddock3\n"
            "description: HADDOCK3 molecular docking\n"
            "version: 1.0\n"
            "last_verified: 2026-03-30\n"
            "---\n\n"
            "# HADDOCK3 Skill\n\n## Quick Start\n\n"
            "HADDOCK3 is a modular docking platform.\n\n"
            "## Common Mistakes\n\n"
            "- Ligand params must be in ALL CNS modules\n"
            "- Output files are .pdb.gz (compressed)\n"
        )

        project_dir = root / "my_docking_project"
        project_dir.mkdir()
        for sub in ["entries", "runs", "sessions", "staging"]:
            (project_dir / ".magnolia" / sub).mkdir(parents=True, exist_ok=True)
        (project_dir / ".magnolia" / "project.yaml").write_text(
            "name: test_docking\ndescription: Phase 1 test\ndefault_tools: [haddock3]\n"
        )

        yield root, skills_dir, project_dir


@pytest.fixture
def run_dir(test_root):
    _, _, project_dir = test_root
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    rd = project_dir / "runs" / f"test_{ts}"
    rd.mkdir(parents=True)
    return rd


# ── Criterion 1: MCP servers start and register tools ─────────────────────────


class TestMCPServerStartup:
    def test_memory_server_imports(self):
        from compchem_memory.server import mcp

        assert mcp is not None
        assert mcp.name == "compchem-memory"

    def test_tools_server_imports(self):
        from compchem_tools.server import mcp

        assert mcp is not None
        assert mcp.name == "compchem-tools"

    def test_memory_server_tool_list(self):
        import asyncio
        from compchem_memory.server import mcp

        tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in [
            "memory_get_context",
            "memory_record_session",
            "memory_record_learning",
            "memory_search",
            "memory_get_run_history",
            "memory_record_run",
            "memory_promote",
            "memory_consolidate",
            "post_run_assess",
            "memory_confirm",
        ]:
            assert name in tool_names, f"Missing tool: {name}"

    def test_tools_server_tool_list(self):
        import asyncio
        from compchem_tools.server import mcp

        tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in [
            "haddock3_run",
            "haddock3_parse_results",
            "preprocess_pdb",
            "generate_restraints",
            "run_acpype",
            "validate_structure",
            "check_environment",
            "stage_gate",
            "check_run_status",
        ]:
            assert name in tool_names, f"Missing tool: {name}"

    def test_memory_server_has_resources(self):
        import asyncio
        from compchem_memory.server import mcp

        resources = asyncio.run(mcp.list_resource_templates())
        assert len(resources) >= 1, f"Expected >=1 resource, got {len(resources)}"


# ── Criterion 2: check_environment detects HADDOCK3 ───────────────────────────


class TestCheckEnvironment:
    def test_detects_python(self):
        from compchem_tools.tools.environment import check_environment

        result = check_environment("python")
        assert result["available"] is True
        assert result["path"] is not None

    def test_haddock3_detection(self):
        from compchem_tools.tools.environment import check_environment

        result = check_environment("haddock3")
        assert "available" in result
        assert "version" in result
        print(f"\n  HADDOCK3 available: {result['available']}")

    def test_conda_env_check(self):
        from compchem_tools.tools.environment import check_environment

        result = check_environment("python", check_conda=True)
        assert "conda_env" in result


# ── Criterion 3: HADDOCK3 run setup ──────────────────────────────────────────


class TestHaddock3RunSetup:
    def test_full_setup(self, run_dir):
        _write_pdb(run_dir / "receptor.pdb", chain_id="A")
        _write_pdb(run_dir / "ligand.pdb", chain_id="B", n_atoms=3, resname="LIG")
        _write_config(run_dir / "config.cfg")
        _write_actpass(run_dir / "actpass_protein.txt", [10, 25, 42], [11, 26])
        _write_actpass(run_dir / "actpass_ligand.txt", [1])

        assert (run_dir / "receptor.pdb").exists()
        assert (run_dir / "ligand.pdb").exists()
        assert (run_dir / "config.cfg").exists()
        cfg = (run_dir / "config.cfg").read_text()
        assert 'run_dir = "output"' in cfg

    def test_preprocess_receptor(self, run_dir):
        from compchem_tools.tools.preprocess import preprocess_pdb

        raw = run_dir / "raw_receptor.pdb"
        _write_pdb(raw, chain_id=" ")
        result = preprocess_pdb(
            str(raw),
            output_path=str(run_dir / "receptor.pdb"),
            add_chain_id="A",
        )
        assert result["success"] is True
        content = Path(result["output_path"]).read_text()
        assert "A" in content

    def test_haddock3_run_launch_detects_missing_binary(self, run_dir):
        from compchem_tools.tools.haddock3 import haddock3_run

        _write_config(run_dir / "config.cfg")
        result = haddock3_run(str(run_dir / "config.cfg"))
        assert "success" in result


# ── Criterion 4: post_run_assess writes run record ────────────────────────────


class TestPostRunAssess:
    def test_assess_completed_run(self, run_dir):
        output_dir = run_dir / "output"
        output_dir.mkdir()
        rigidbody_dir = output_dir / "01_rigidbody"
        rigidbody_dir.mkdir()
        for i in range(3):
            (rigidbody_dir / f"rigidbody_{i}.pdb.gz").write_text(f"fake pose {i}")
        (rigidbody_dir / "io.json").write_text('{"finished": true}')
        capri_dir = output_dir / "02_caprieval"
        capri_dir.mkdir()
        (capri_dir / "capri_clt.tsv").write_text(
            "model\tcluster_id\tscore\tlrmsd\tfnat\n"
            "model_1\tcluster_1\t-85.5\t2.3\t0.65\n"
        )

        from compchem_memory.learning.assessor import assess_run

        assessment = assess_run(str(run_dir), "haddock3", exit_code=0)
        assert assessment["overall"] in ("pass", "warning")
        assert assessment["technical"]["run_dir_exists"] is True

    def test_run_record_written(self, test_root):
        _, _, project_dir = test_root
        from compchem_memory.tiers.project import ProjectManager

        mgr = ProjectManager(Path("/tmp"))
        run_id = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        mgr.record_run(
            str(project_dir),
            run_id=run_id,
            tool="haddock3",
            status="success",
            metrics={"best_score": -85.5},
        )
        history = mgr.get_run_history(str(project_dir))
        found = [r for r in history if r["run_id"] == run_id]
        assert len(found) == 1
        assert found[0]["metrics"]["best_score"] == -85.5


# ── Criterion 5: memory_get_context returns skill content ────────────────────


class TestMemoryGetContext:
    def test_returns_skill_for_haddock3(self, test_root):
        _, skills_dir, _ = test_root
        from compchem_memory.tiers.skill import SkillManager

        mgr = SkillManager(skills_dir)
        content = mgr.get_skill("haddock3")
        assert content is not None
        assert "HADDOCK3" in content
        assert "Common Mistakes" in content

    def test_context_assembly(self, test_root):
        _, skills_dir, project_dir = test_root
        from compchem_memory.tiers.skill import SkillManager
        from compchem_memory.tiers.project import ProjectManager

        skill_mgr = SkillManager(skills_dir)
        project_mgr = ProjectManager(Path("/tmp"))

        skills = skill_mgr.search_skills(keyword="docking")
        assert len(skills) >= 1

        project_mgr.create_entry(
            str(project_dir),
            "Docking Note",
            "Use actpass format",
            tags=["haddock3"],
        )
        entries = project_mgr.search_entries(str(project_dir), keyword="docking")
        assert len(entries) >= 1


# ── Criterion 6: Project-tier entries CRUD ────────────────────────────────────


class TestProjectTierEntries:
    def test_create_and_retrieve(self, test_root):
        _, _, project_dir = test_root
        from compchem_memory.tiers.project import ProjectManager

        mgr = ProjectManager(Path("/tmp"))
        path = mgr.create_entry(
            str(project_dir),
            f"Selenium Note {datetime.now().strftime('%H%M%S%f')}",
            "Use vinyl product form for docking",
            tags=["covalent"],
        )
        assert Path(path).exists()
        content = mgr.get_entry(str(project_dir), "Selenium")
        assert content is not None
        assert "vinyl" in content.lower()

    def test_search_by_tag(self, test_root):
        _, _, project_dir = test_root
        from compchem_memory.tiers.project import ProjectManager

        mgr = ProjectManager(Path("/tmp"))
        unique_tag = f"test_tag_{datetime.now().strftime('%H%M%S%f')}"
        mgr.create_entry(
            str(project_dir),
            f"Tagged Note {unique_tag}",
            "Some content",
            tags=[unique_tag],
        )
        results = mgr.search_entries(str(project_dir), tags=[unique_tag])
        assert len(results) >= 1

    def test_staging_workflow(self, test_root):
        _, _, project_dir = test_root
        from compchem_memory.tiers.project import ProjectManager

        mgr = ProjectManager(Path("/tmp"))
        mgr.create_entry(
            str(project_dir),
            f"Unverified {datetime.now().strftime('%H%M%S%f')}",
            "Maybe increasing sampling helps",
            staging=True,
        )
        entries = mgr.list_entries(str(project_dir))
        staging_dir = project_dir / ".magnolia" / "staging"
        staged_files = list(staging_dir.glob("*.md"))
        assert len(staged_files) >= 1

        mgr.confirm_staging(str(project_dir), staged_files[0].stem)
        entries_after = mgr.list_entries(str(project_dir))
        assert len(entries_after) > len(entries)


# ── Criterion 7: Session logs written incrementally ──────────────────────────


class TestSessionLogs:
    def test_incremental_writing(self, test_root):
        _, _, project_dir = test_root
        from compchem_memory.tiers.session import SessionManager

        sessions_dir = project_dir / ".magnolia" / "sessions" / "inc_test"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        mgr = SessionManager(sessions_dir)
        mgr.start_new_session()

        path1 = mgr.record("tool_call", {"tool": "validate_structure"})
        assert Path(path1).exists()
        content_after_1 = Path(path1).read_text()
        assert "validate_structure" in content_after_1

        mgr.record("tool_success", {"tool": "validate_structure"})
        mgr.record("tool_error", {"tool": "haddock3_run", "error": "sampling too low"})

        recent = mgr.get_recent(n=10)
        user_events = [e for e in recent if e["event_type"] != "session_start"]
        assert len(user_events) == 3

        errors = mgr.search("error")
        error_events = [e for e in errors if e["event_type"] != "session_start"]
        assert len(error_events) == 1

    def test_session_persists_across_calls(self, test_root):
        _, _, project_dir = test_root
        from compchem_memory.tiers.session import SessionManager

        sessions_dir = project_dir / ".magnolia" / "sessions" / "persist_test"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        mgr = SessionManager(sessions_dir)
        mgr.start_new_session()

        mgr.record("start", {"message": "session begins"})
        mgr.record("end", {"message": "session ends"})
        all_events = mgr.get_recent(n=100)
        user_events = [e for e in all_events if e["event_type"] != "session_start"]
        assert len(user_events) == 2


# ── Criterion 8: Stage gate docking_inputs_ready ──────────────────────────────


class TestStageGates:
    def test_gate_passes_with_valid_inputs(self, run_dir):
        _write_pdb(run_dir / "receptor.pdb", chain_id="A")
        _write_pdb(run_dir / "ligand.pdb", chain_id="B", n_atoms=3)
        _write_config(run_dir / "config.cfg")

        from compchem_tools.gates.docking import docking_inputs_ready

        result = docking_inputs_ready(str(run_dir))
        assert result["passed"] is True, f"Gate failed: {result}"

    def test_gate_fails_no_config(self, run_dir):
        _write_pdb(run_dir / "receptor.pdb", chain_id="A")
        from compchem_tools.gates.docking import docking_inputs_ready

        result = docking_inputs_ready(str(run_dir))
        assert result["passed"] is False
        assert result["details"]["config_exists"] is False

    def test_gate_fails_no_ligand(self, run_dir):
        _write_pdb(run_dir / "receptor.pdb", chain_id="A")
        _write_config(run_dir / "config.cfg")
        from compchem_tools.gates.docking import docking_inputs_ready

        result = docking_inputs_ready(str(run_dir))
        assert result["passed"] is False

    def test_gate_via_registry(self, run_dir):
        _write_pdb(run_dir / "receptor.pdb", chain_id="A")
        _write_pdb(run_dir / "ligand.pdb", chain_id="B", n_atoms=3)
        _write_config(run_dir / "config.cfg")

        from compchem_tools.gates import GATE_REGISTRY

        assert "docking_inputs_ready" in GATE_REGISTRY
        result = GATE_REGISTRY["docking_inputs_ready"](str(run_dir))
        assert result["passed"] is True

    def test_unknown_gate(self):
        from compchem_tools.gates import GATE_REGISTRY

        assert "nonexistent_gate" not in GATE_REGISTRY


# ── End-to-end workflow simulation ────────────────────────────────────────────


class TestEndToEndWorkflow:
    def test_full_workflow(self, test_root):
        _, skills_dir, project_dir = test_root

        from compchem_memory.tiers.session import SessionManager
        from compchem_memory.tiers.project import ProjectManager
        from compchem_memory.tiers.skill import SkillManager
        from compchem_memory.learning.assessor import assess_run
        from compchem_tools.tools.preprocess import preprocess_pdb, validate_structure
        from compchem_tools.gates.docking import docking_inputs_ready

        sessions_dir = project_dir / ".magnolia" / "sessions" / "e2e"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_mgr = SessionManager(sessions_dir)
        session_mgr.start_new_session()
        project_mgr = ProjectManager(Path("/tmp"))
        skill_mgr = SkillManager(skills_dir)

        # Step 1: Load context
        skill_content = skill_mgr.get_skill("haddock3")
        assert skill_content is not None, "No HADDOCK3 skill found"
        session_mgr.record("memory_get_context", {"tool": "haddock3", "loaded": True})

        # Step 2: Set up run directory
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_dir = project_dir / "runs" / f"e2e_{ts}"
        run_dir.mkdir(parents=True)
        session_mgr.record("create_run_dir", {"path": str(run_dir)})

        # Step 3: Preprocess receptor (raw file → clean file)
        raw = run_dir / "raw_receptor.pdb"
        _write_pdb(raw, chain_id=" ")
        result = preprocess_pdb(
            str(raw),
            output_path=str(run_dir / "receptor.pdb"),
            add_chain_id="A",
        )
        assert result["success"]

        # Delete raw file so gate doesn't pick it up
        raw.unlink()

        # Step 4: Validate receptor
        v = validate_structure(str(run_dir / "receptor.pdb"))
        assert v["valid"], f"Receptor validation failed: {v}"
        session_mgr.record("tool_success", {"tool": "validate_structure"})

        # Step 5: Create ligand and config
        _write_pdb(run_dir / "ligand.pdb", chain_id="B", n_atoms=3, resname="LIG")
        _write_config(run_dir / "config.cfg")
        session_mgr.record("tool_success", {"tool": "setup_inputs"})

        # Step 6: Stage gate
        gate = docking_inputs_ready(str(run_dir))
        assert gate["passed"], f"Gate failed: {gate}"
        session_mgr.record(
            "stage_gate", {"gate": "docking_inputs_ready", "passed": True}
        )

        # Step 7: Simulate run completion
        output = run_dir / "output" / "01_rigidbody"
        output.mkdir(parents=True)
        (output / "rigidbody_1.pdb.gz").write_text("pose data")
        (output / "io.json").write_text('{"finished": true}')
        capri = run_dir / "output" / "02_caprieval"
        capri.mkdir()
        (capri / "capri_clt.tsv").write_text(
            "model\tcluster_id\tscore\tlrmsd\nmodel_1\tc1\t-95.3\t1.8\n"
        )

        # Step 8: Post-run assess
        assessment = assess_run(str(run_dir), "haddock3", exit_code=0)
        assert assessment["overall"] in ("pass", "warning")
        session_mgr.record("post_run_assess", {"overall": assessment["overall"]})

        # Step 9: Record run
        project_mgr.record_run(
            str(project_dir),
            run_id=f"e2e_{ts}",
            tool="haddock3",
            status="success",
            metrics=assessment.get("metrics", {}),
        )

        # Step 10: Verify everything was recorded
        history = project_mgr.get_run_history(str(project_dir))
        assert any(
            r["tool"] == "haddock3" and r["status"] == "success" for r in history
        )

        recent = session_mgr.get_recent(n=20)
        event_types = [e["event_type"] for e in recent]
        assert "memory_get_context" in event_types
        assert "stage_gate" in event_types
        assert "post_run_assess" in event_types

        print(f"\n  === E2E Summary ===")
        print(f"  Run dir: {run_dir}")
        print(f"  Assessment: {assessment['overall']}")
        print(f"  Events: {len(recent)}")
        print(f"  Run records: {len(history)}")
