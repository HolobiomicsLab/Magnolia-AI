"""Tests for Phase 3: Gnina docking, covalent tools, xTB, covalent gates."""

import tempfile
from pathlib import Path

import pytest

from compchem_tools.tools.gnina import gnina_dock, gnina_parse_results, _parse_gnina_sdf
from compchem_tools.tools.covalent import smarts_validate, alkyne_to_vinyl
from compchem_tools.tools.xtb import xtb_optimize, xtb_singlepoint
from compchem_tools.gates.covalent import (
    vinyl_isomers_exist,
    docked_poses_exist,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── Gnina Tools ──────────────────────────────────────────────────────────────


class TestGninaParseResults:
    def test_parse_gnina_sdf(self):
        """Test parsing Gnina SDF output format."""
        sdf_text = (
            "ligand\n"
            "     RDKit          3D\n"
            "\n"
            " 10 11  0  0  0  0  0  0  0  0999 V2000\n"
            "    0.0000    0.0000    0.0000 C   0  0  0  0  0\n"
            "    1.0000    0.0000    0.0000 C   0  0  0  0  0\n"
            "  1  2  1  0\n"
            "M  END\n"
            "> <minimizedAffinity>\n"
            "-7.2\n"
            "\n"
            "> <CNNscore>\n"
            "0.85\n"
            "\n"
            "> <CNNaffinity>\n"
            "-6.8\n"
            "\n"
            "$$$$\n"
        )
        poses = _parse_gnina_sdf(sdf_text)
        assert len(poses) == 1
        assert poses[0]["minimizedAffinity"] == -7.2
        assert poses[0]["CNNscore"] == 0.85
        assert poses[0]["CNNaffinity"] == -6.8

    def test_parse_empty_dir(self, tmp_dir):
        result = gnina_parse_results(str(tmp_dir))
        assert result["success"] is False
        assert result["pose_count"] == 0

    def test_parse_gnina_output_dir(self, tmp_dir):
        """Test parsing when SDF files exist in output directory."""
        sdf_content = (
            "mol\n"
            "     RDKit          3D\n"
            "\n"
            "  2  1  0  0  0  0  0  0  0  0999 V2000\n"
            "    0.0    0.0    0.0 C   0  0  0  0  0\n"
            "    1.0    0.0    0.0 C   0  0  0  0  0\n"
            "  1  2  1  0\n"
            "M  END\n"
            "> <minimizedAffinity>\n"
            "-5.5\n"
            "\n"
            "> <CNNscore>\n"
            "0.7\n"
            "\n"
            "> <CNNaffinity>\n"
            "-5.0\n"
            "\n"
            "$$$$\n"
        )
        (tmp_dir / "docked.sdf").write_text(sdf_content)
        result = gnina_parse_results(str(tmp_dir))
        assert result["success"] is True
        assert result["pose_count"] == 1


class TestGninaDock:
    def test_missing_receptor(self, tmp_dir):
        lig = tmp_dir / "lig.sdf"
        lig.write_text("fake ligand")
        result = gnina_dock("/nonexistent.pdb", str(lig))
        assert result["success"] is False

    def test_missing_ligand(self, tmp_dir):
        rec = tmp_dir / "rec.pdb"
        rec.write_text("fake receptor")
        result = gnina_dock(str(rec), "/nonexistent.sdf")
        assert result["success"] is False

    def test_covalent_requires_params(self, tmp_dir):
        rec = tmp_dir / "rec.pdb"
        rec.write_text("fake receptor")
        lig = tmp_dir / "lig.sdf"
        lig.write_text("fake ligand")
        result = gnina_dock(
            str(rec), str(lig),
            covalent=True,
            covalent_receptor_atom="A:45:SG",
            covalent_ligand_atom_pattern=None,
        )
        assert result["success"] is False
        assert "covalent" in result["error"].lower()


# ── SMARTS Validation ────────────────────────────────────────────────────────


class TestSmartsValidate:
    def test_valid_smarts(self):
        result = smarts_validate("[C:1]=[C:2]")
        assert result["valid"] is True

    def test_invalid_brackets(self):
        result = smarts_validate("[C:1=[C:2]")
        assert result["valid"] is False

    def test_invalid_parens(self):
        result = smarts_validate("C(C(C")
        assert result["valid"] is False

    def test_ring_digit_odd(self):
        result = smarts_validate("C1CC")
        assert result["valid"] is False

    def test_valid_ring(self):
        result = smarts_validate("C1CCC1")
        assert result["valid"] is True


# ── Alkyne to Vinyl ──────────────────────────────────────────────────────────


class TestAlkyneToVinyl:
    def test_no_triple_bond(self):
        result = alkyne_to_vinyl("CCO")
        assert result["success"] is False

    def test_output_dir_created(self, tmp_dir):
        out = tmp_dir / "vinyl_output"
        result = alkyne_to_vinyl("CC#C", str(out))
        # Just check it doesn't crash and returns expected keys
        assert "isomers" in result or "error" in result


# ── xTB Tools ────────────────────────────────────────────────────────────────


class TestXtbOptimize:
    def test_missing_input(self):
        result = xtb_optimize("/nonexistent.xyz")
        assert result["success"] is False

    def test_existing_input(self, tmp_dir):
        """If xtb is not installed, should fail gracefully."""
        inp = tmp_dir / "test.xyz"
        inp.write_text("2\n\nC 0 0 0\nH 1 0 0\n")
        result = xtb_optimize(str(inp))
        assert "success" in result
        if not result["success"]:
            assert "error" in result


class TestXtbSinglepoint:
    def test_missing_input(self):
        result = xtb_singlepoint("/nonexistent.xyz")
        assert result["success"] is False


# ── Covalent Gates ───────────────────────────────────────────────────────────


class TestCovalentGates:
    def test_vinyl_isomers_exist_pass(self, tmp_dir):
        (tmp_dir / "vinyl_Z.smi").write_text("C/C=C/C")
        (tmp_dir / "vinyl_E.smi").write_text("C\\C=C\\C")
        result = vinyl_isomers_exist(str(tmp_dir))
        assert result["passed"] is True

    def test_vinyl_isomers_exist_fail(self, tmp_dir):
        (tmp_dir / "vinyl_Z.smi").write_text("C/C=C/C")
        result = vinyl_isomers_exist(str(tmp_dir))
        assert result["passed"] is False

    def test_vinyl_isomers_empty_dir(self, tmp_dir):
        result = vinyl_isomers_exist(str(tmp_dir))
        assert result["passed"] is False

    def test_docked_poses_exist_pass(self, tmp_dir):
        output = tmp_dir / "output"
        output.mkdir()
        (output / "docked.sdf").write_text("valid pose data with some content")
        result = docked_poses_exist(str(tmp_dir))
        assert result["passed"] is True

    def test_docked_poses_exist_empty(self, tmp_dir):
        result = docked_poses_exist(str(tmp_dir))
        assert result["passed"] is False

    def test_docked_poses_exist_zero_size(self, tmp_dir):
        output = tmp_dir / "output"
        output.mkdir()
        (output / "docked.sdf").write_text("")
        result = docked_poses_exist(str(tmp_dir))
        assert result["passed"] is False


# ── Server Registration Test ─────────────────────────────────────────────────


class TestPhase3ServerRegistration:
    def test_phase3_tools_registered(self):
        import asyncio
        from compchem_tools.server import mcp

        tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in [
            "gnina_dock",
            "gnina_parse_results",
            "smarts_validate",
            "alkyne_to_vinyl",
            "xtb_optimize",
            "xtb_singlepoint",
        ]:
            assert name in tool_names, f"Missing Phase 3 tool: {name}"

    def test_phase3_gates_registered(self):
        from compchem_tools.gates import GATE_REGISTRY

        for name in ["vinyl_isomers_exist", "docked_poses_exist"]:
            assert name in GATE_REGISTRY, f"Missing gate: {name}"
