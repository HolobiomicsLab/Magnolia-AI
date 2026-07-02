"""Tests for Phase 4: Workflow templates, P2Rank, GROMACS, workflow status."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from compchem_tools.tools.workflow import load_workflow, get_workflow_status
from compchem_tools.tools.p2rank import p2rank_predict, _parse_predictions_csv
from compchem_tools.tools.gromacs import gromacs_setup, gromacs_run, gromacs_parse


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── Workflow Template Loading ─────────────────────────────────────────────────


class TestLoadWorkflow:
    def test_valid_template(self, tmp_dir):
        tpl = tmp_dir / "workflow.yaml"
        tpl.write_text(
            "name: test_workflow\n"
            "description: A test workflow\n"
            "steps:\n"
            "  - name: step_one\n"
            "    tool: p2rank_predict\n"
            "    outputs: [predictions.csv]\n"
            "  - name: step_two\n"
            "    tool: haddock3_run\n"
            "    depends_on: [step_one]\n"
            "    outputs: [output/]\n"
        )
        result = load_workflow(str(tpl))
        assert result["success"] is True
        assert result["name"] == "test_workflow"
        assert len(result["steps"]) == 2

    def test_missing_file(self):
        result = load_workflow("/nonexistent/workflow.yaml")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_missing_name_key(self, tmp_dir):
        tpl = tmp_dir / "bad.yaml"
        tpl.write_text(
            "steps:\n"
            "  - name: step_one\n"
            "    tool: test\n"
        )
        result = load_workflow(str(tpl))
        assert result["success"] is False
        assert "name" in result["error"]

    def test_missing_steps_key(self, tmp_dir):
        tpl = tmp_dir / "bad.yaml"
        tpl.write_text("name: no_steps\n")
        result = load_workflow(str(tpl))
        assert result["success"] is False
        assert "steps" in result["error"]

    def test_empty_steps(self, tmp_dir):
        tpl = tmp_dir / "bad.yaml"
        tpl.write_text("name: empty\nsteps: []\n")
        result = load_workflow(str(tpl))
        assert result["success"] is False

    def test_step_missing_name(self, tmp_dir):
        tpl = tmp_dir / "bad.yaml"
        tpl.write_text(
            "name: bad_step\n"
            "steps:\n"
            "  - tool: test\n"
        )
        result = load_workflow(str(tpl))
        assert result["success"] is False
        assert "name" in result["error"]

    def test_step_missing_tool(self, tmp_dir):
        tpl = tmp_dir / "bad.yaml"
        tpl.write_text(
            "name: bad_step\n"
            "steps:\n"
            "  - name: step_one\n"
        )
        result = load_workflow(str(tpl))
        assert result["success"] is False
        assert "tool" in result["error"]

    def test_duplicate_step_names(self, tmp_dir):
        tpl = tmp_dir / "dup.yaml"
        tpl.write_text(
            "name: dup\n"
            "steps:\n"
            "  - name: step_one\n"
            "    tool: tool_a\n"
            "  - name: step_one\n"
            "    tool: tool_b\n"
        )
        result = load_workflow(str(tpl))
        assert result["success"] is False
        assert "Duplicate" in result["error"]

    def test_invalid_depends_on(self, tmp_dir):
        tpl = tmp_dir / "bad_dep.yaml"
        tpl.write_text(
            "name: bad_dep\n"
            "steps:\n"
            "  - name: step_one\n"
            "    tool: tool_a\n"
            "  - name: step_two\n"
            "    tool: tool_b\n"
            "    depends_on: [nonexistent_step]\n"
        )
        result = load_workflow(str(tpl))
        assert result["success"] is False
        assert "unknown step" in result["error"]

    def test_description_optional(self, tmp_dir):
        tpl = tmp_dir / "no_desc.yaml"
        tpl.write_text(
            "name: minimal\n"
            "steps:\n"
            "  - name: step_one\n"
            "    tool: test_tool\n"
        )
        result = load_workflow(str(tpl))
        assert result["success"] is True
        assert result["description"] == ""

    def test_pocket_docking_template(self):
        """Test loading the actual pocket_docking workflow template."""
        import os
        tpl_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "workflows", "pocket_docking.yaml"
        )
        tpl_path = os.path.abspath(tpl_path)
        if os.path.exists(tpl_path):
            result = load_workflow(tpl_path)
            assert result["success"] is True
            assert result["name"] == "pocket_docking"
            assert len(result["steps"]) == 2

    def test_dock_then_md_template(self):
        """Test loading the actual dock_then_md workflow template."""
        import os
        tpl_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "workflows", "dock_then_md.yaml"
        )
        tpl_path = os.path.abspath(tpl_path)
        if os.path.exists(tpl_path):
            result = load_workflow(tpl_path)
            assert result["success"] is True
            assert result["name"] == "dock_then_md"
            assert len(result["steps"]) == 5


# ── P2Rank Predict ───────────────────────────────────────────────────────────


class TestP2rankPredict:
    def test_missing_input(self):
        result = p2rank_predict("/nonexistent.pdb")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_existing_input_no_binary(self, tmp_dir):
        """If p2rank is not installed, should fail gracefully."""
        pdb = tmp_dir / "test.pdb"
        pdb.write_text(
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        result = p2rank_predict(str(pdb))
        assert "success" in result
        if not result["success"]:
            assert "error" in result

    def test_output_dir_created(self, tmp_dir):
        """Output directory should be created even if the binary is missing."""
        pdb = tmp_dir / "test.pdb"
        pdb.write_text("ATOM data\nEND\n")
        out_dir = tmp_dir / "p2rank_out"
        result = p2rank_predict(str(pdb), output_dir=str(out_dir))
        assert out_dir.exists()


class TestP2rankParseCSV:
    def test_parse_mock_csv(self, tmp_dir):
        """Test parsing a mock P2Rank predictions CSV."""
        csv_content = (
            "name,rank,score,probability,center_x,center_y,center_z,residue_count,volume,residues\n"
            "pocket1,1,0.85,0.92,15.3,22.1,8.7,12,245.6,A45 A46 A47 A48 B12 B13\n"
            "pocket2,2,0.62,0.71,5.1,10.0,3.2,8,180.3,B22 B23 B24 C1 C2\n"
        )
        csv_file = tmp_dir / "predictions.csv"
        csv_file.write_text(csv_content)

        pockets = _parse_predictions_csv(csv_file)
        assert len(pockets) == 2
        assert pockets[0]["rank"] == 1
        assert pockets[0]["score"] == 0.85
        assert len(pockets[0]["residue_list"]) == 6
        assert pockets[1]["rank"] == 2
        assert pockets[1]["residue_count"] == 8

    def test_parse_empty_csv(self, tmp_dir):
        csv_file = tmp_dir / "empty.csv"
        csv_file.write_text("name,rank,score\n")
        pockets = _parse_predictions_csv(csv_file)
        assert len(pockets) == 0

    def test_parse_csv_with_comments(self, tmp_dir):
        csv_content = (
            "# P2Rank predictions\n"
            "# Generated: 2026-04-01\n"
            "name,rank,score,probability\n"
            "pocket1,1,0.75,0.80\n"
        )
        csv_file = tmp_dir / "predictions.csv"
        csv_file.write_text(csv_content)
        pockets = _parse_predictions_csv(csv_file)
        assert len(pockets) == 1
        assert pockets[0]["score"] == 0.75


# ── GROMACS Setup ────────────────────────────────────────────────────────────


class TestGromacsSetup:
    def test_missing_structure(self):
        result = gromacs_setup("/nonexistent.pdb")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_existing_structure_no_binary(self, tmp_dir):
        """If gmx is not installed, should fail gracefully."""
        pdb = tmp_dir / "test.pdb"
        pdb.write_text(
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        result = gromacs_setup(str(pdb))
        assert "success" in result
        if not result["success"]:
            assert "error" in result

    def test_output_dir_created(self, tmp_dir):
        pdb = tmp_dir / "test.pdb"
        pdb.write_text("ATOM data\nEND\n")
        out_dir = tmp_dir / "gmx_out"
        result = gromacs_setup(str(pdb), output_dir=str(out_dir))
        assert out_dir.exists()


# ── GROMACS Run ──────────────────────────────────────────────────────────────


class TestGromacsRun:
    def test_missing_tpr(self):
        result = gromacs_run("/nonexistent.tpr")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_existing_tpr_no_binary(self, tmp_dir):
        """If gmx is not installed, should fail gracefully."""
        tpr = tmp_dir / "md.tpr"
        tpr.write_text("fake tpr data")
        result = gromacs_run(str(tpr))
        assert "success" in result
        if not result["success"]:
            assert "error" in result


# ── GROMACS Parse ────────────────────────────────────────────────────────────


class TestGromacsParse:
    def test_no_inputs(self):
        result = gromacs_parse()
        assert result["success"] is False
        assert "at least one" in result["error"]

    def test_missing_energy_file(self):
        result = gromacs_parse(energy_file="/nonexistent.edr")
        assert result["success"] is False

    def test_missing_trajectory(self):
        result = gromacs_parse(trajectory="/nonexistent.xtc")
        assert result["success"] is False

    def test_missing_both_specified(self):
        result = gromacs_parse(
            energy_file="/nonexistent.edr",
            trajectory="/nonexistent.xtc"
        )
        assert result["success"] is False


# ── Workflow Status ──────────────────────────────────────────────────────────


class TestGetWorkflowStatus:
    def test_missing_run_dir(self):
        result = get_workflow_status("/nonexistent/dir")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_no_plan_file(self, tmp_dir):
        result = get_workflow_status(str(tmp_dir))
        assert result["success"] is False
        assert "workflow_plan" in result["error"]

    def test_valid_plan_with_outputs(self, tmp_dir):
        """Test workflow status when some outputs exist."""
        # Write the plan
        plan = tmp_dir / "workflow_plan.json"
        plan.write_text(json.dumps({
            "name": "test_workflow",
            "steps": [
                {
                    "name": "step_one",
                    "tool": "test_tool",
                    "outputs": ["output.txt"],
                },
                {
                    "name": "step_two",
                    "tool": "test_tool",
                    "outputs": ["result.csv"],
                    "depends_on": ["step_one"],
                },
            ],
        }))

        # Create output for step_one
        step_dir = tmp_dir / "step_one"
        step_dir.mkdir()
        (step_dir / "output.txt").write_text("results")

        result = get_workflow_status(str(tmp_dir))
        assert result["success"] is True
        assert result["total_steps"] == 2
        assert result["completed_steps"] == 1
        assert result["all_complete"] is False

    def test_all_steps_complete(self, tmp_dir):
        """Test workflow status when all outputs exist."""
        plan = tmp_dir / "workflow_plan.json"
        plan.write_text(json.dumps({
            "name": "complete_workflow",
            "steps": [
                {
                    "name": "step_a",
                    "tool": "test",
                    "outputs": ["out.dat"],
                },
            ],
        }))

        step_dir = tmp_dir / "step_a"
        step_dir.mkdir()
        (step_dir / "out.dat").write_text("done")

        result = get_workflow_status(str(tmp_dir))
        assert result["success"] is True
        assert result["all_complete"] is True
        assert result["completed_steps"] == 1

    def test_yaml_plan_file(self, tmp_dir):
        """Test workflow status with a YAML plan file."""
        plan = tmp_dir / "workflow_plan.yaml"
        plan.write_text(
            "name: yaml_test\n"
            "steps:\n"
            "  - name: predict\n"
            "    tool: p2rank_predict\n"
            "    outputs: [predictions.csv]\n"
        )

        # Create the output
        step_dir = tmp_dir / "predict"
        step_dir.mkdir()
        (step_dir / "predictions.csv").write_text("name,rank\np1,1\n")

        result = get_workflow_status(str(tmp_dir))
        assert result["success"] is True
        assert result["workflow"] == "yaml_test"
        assert result["all_complete"] is True


# ── Server Registration Test ─────────────────────────────────────────────────


class TestPhase4ServerRegistration:
    def test_phase4_tools_registered(self):
        import asyncio
        from compchem_tools.server import mcp

        tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in [
            "workflow_load",
            "workflow_status",
            "p2rank_predict",
            "gromacs_setup",
            "gromacs_run",
            "gromacs_parse",
        ]:
            assert name in tool_names, f"Missing Phase 4 tool: {name}"
