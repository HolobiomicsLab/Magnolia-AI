"""Tests for compchem-tools: structure validation, gates, environment."""

import json
import tempfile
from pathlib import Path

import pytest

from compchem_tools.tools.preprocess import preprocess_pdb, validate_structure
from compchem_tools.tools.haddock3 import generate_restraints
from compchem_tools.gates.docking import docking_inputs_ready, pose_valid
from compchem_tools.gates.structure import pdb_has_chain_id, file_size_nonzero


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _write_pdb(path: Path, chain_id: str = "A", atom_count: int = 5) -> None:
    lines = []
    for i in range(atom_count):
        serial = i + 1
        x, y, z = i * 1.0, i * 1.0, i * 1.0
        line = (
            f"ATOM  {serial:>5d}  CA  ALA {chain_id}{serial:>4d}    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C"
        )
        lines.append(line)
    lines.append("END")
    path.write_text("\n".join(lines))


class TestValidateStructure:
    def test_valid_pdb(self, tmp_dir):
        pdb = tmp_dir / "test.pdb"
        _write_pdb(pdb, chain_id="A")
        result = validate_structure(str(pdb))
        assert result["valid"] is True
        assert result["atom_count"] == 5
        assert "A" in result["chain_ids"]

    def test_empty_file(self, tmp_dir):
        pdb = tmp_dir / "empty.pdb"
        pdb.write_text("")
        result = validate_structure(str(pdb))
        assert result["valid"] is False

    def test_no_chain_id(self, tmp_dir):
        pdb = tmp_dir / "nochain.pdb"
        lines = [
            "ATOM      1  CA  ALA     1       0.000   0.000   0.000  1.00  0.00           C",
            "END",
        ]
        pdb.write_text("\n".join(lines))
        result = validate_structure(str(pdb))
        assert result["valid"] is False or "No chain IDs" in str(
            result.get("issues", [])
        )

    def test_nonexistent_file(self):
        result = validate_structure("/nonexistent.pdb")
        assert result["valid"] is False

    def test_sdf_file(self, tmp_dir):
        sdf = tmp_dir / "test.sdf"
        sdf.write_text("\nheader\n\n  3  2  0  0\natoms\nM  END\n$$$$\n")
        result = validate_structure(str(sdf))
        assert result["format"] == "sdf"


class TestPreprocessPdb:
    def test_remove_waters(self, tmp_dir):
        pdb = tmp_dir / "test.pdb"
        lines = [
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C",
            "HETATM    2  O   HOH A 100       1.000   1.000   1.000  1.00  0.00           O",
            "END",
        ]
        pdb.write_text("\n".join(lines))

        result = preprocess_pdb(str(pdb), remove_waters=True)
        assert result["success"] is True
        assert result["atom_count"] == 1
        assert result["removed_waters"] == 1

    def test_add_chain_id(self, tmp_dir):
        pdb = tmp_dir / "test.pdb"
        pdb.write_text(
            "ATOM    1  CA  ALA    1       0.000   0.000   0.000  1.00  0.00           C\nEND\n"
        )
        result = preprocess_pdb(str(pdb), add_chain_id="B")
        assert result["success"] is True
        output = Path(result["output_path"]).read_text()
        assert "B" in output[21:22] or "B" in output


class TestStageGates:
    def test_docking_inputs_ready_pass(self, tmp_dir):
        _write_pdb(tmp_dir / "receptor.pdb", chain_id="A")
        (tmp_dir / "ligand.sdf").write_text("fake ligand\nM  END\n$$$$\n")
        (tmp_dir / "config.cfg").write_text("run_dir = 'output'")
        result = docking_inputs_ready(str(tmp_dir))
        assert result["passed"] is True

    def test_docking_inputs_ready_no_config(self, tmp_dir):
        _write_pdb(tmp_dir / "receptor.pdb")
        result = docking_inputs_ready(str(tmp_dir))
        assert result["passed"] is False
        assert result["details"]["config_exists"] is False

    def test_pose_valid_with_files(self, tmp_dir):
        output = tmp_dir / "output"
        output.mkdir()
        (output / "best.pdb").write_text("ATOM data\nEND\n")
        result = pose_valid(str(tmp_dir))
        assert result["passed"] is True
        assert result["details"]["pose_count"] == 1

    def test_pose_valid_empty(self, tmp_dir):
        result = pose_valid(str(tmp_dir))
        assert result["passed"] is False

    def test_pdb_has_chain_id(self, tmp_dir):
        pdb = tmp_dir / "test.pdb"
        _write_pdb(pdb, chain_id="A")
        result = pdb_has_chain_id(str(pdb), "A")
        assert result["passed"] is True

    def test_file_size_nonzero(self, tmp_dir):
        f = tmp_dir / "test.txt"
        f.write_text("content")
        result = file_size_nonzero(str(f))
        assert result["passed"] is True

    def test_file_size_zero(self, tmp_dir):
        f = tmp_dir / "empty.txt"
        f.write_text("")
        result = file_size_nonzero(str(f))
        assert result["passed"] is False


class TestGenerateRestraints:
    def test_actpass_validation_wrong_lines(self, tmp_dir):
        ap1 = tmp_dir / "bad_actpass.txt"
        ap1.write_text("10 20 30")
        ap2 = tmp_dir / "ok_actpass.txt"
        ap2.write_text("10 20 30\n40 50 60\n")
        out = tmp_dir / "ambig.tbl"
        result = generate_restraints(str(ap1), str(ap2), str(out))
        assert result["success"] is False
        assert "2 lines" in result["error"]

    def test_actpass_file_not_found(self, tmp_dir):
        out = tmp_dir / "ambig.tbl"
        result = generate_restraints("/nonexistent1.txt", "/nonexistent2.txt", str(out))
        assert result["success"] is False
