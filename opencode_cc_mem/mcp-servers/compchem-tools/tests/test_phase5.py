"""Tests for Phase 5: ORCA, Gaussian, Job Management, QM Gates."""

import json
import tempfile
from pathlib import Path

import pytest

from compchem_tools.tools.orca import orca_setup, orca_run, orca_parse
from compchem_tools.tools.gaussian import gaussian_setup, gaussian_run, gaussian_parse
from compchem_tools.tools.jobs import submit_job, check_job, cancel_job
from compchem_tools.gates.structure import qm_inputs_defined, scf_converged


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── ORCA Setup ───────────────────────────────────────────────────────────────


class TestOrcaSetup:
    def test_generates_valid_input_file(self, tmp_dir):
        """Test that orca_setup generates a valid ORCA .inp file."""
        xyz_file = tmp_dir / "molecule.xyz"
        xyz_file.write_text("2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n")

        result = orca_setup(
            str(xyz_file),
            method="B3LYP",
            basis="def2-SVP",
            charge=0,
            multiplicity=1,
            task="SP",
            ncores=4,
        )

        assert result["success"] is True
        assert "input_file" in result
        assert result["method"] == "B3LYP"
        assert result["basis"] == "def2-SVP"
        assert result["charge"] == 0
        assert result["multiplicity"] == 1

        # Check file was actually created
        inp_path = Path(result["input_file"])
        assert inp_path.exists()
        content = inp_path.read_text()
        assert "B3LYP" in content
        assert "def2-SVP" in content
        assert "nprocs 4" in content

    def test_with_solvent(self, tmp_dir):
        """Test ORCA setup with implicit solvent."""
        xyz_file = tmp_dir / "mol.xyz"
        xyz_file.write_text("1\n\nH 0.0 0.0 0.0\n")

        result = orca_setup(
            str(xyz_file),
            method="B3LYP",
            basis="def2-SVP",
            solvent="water",
        )

        assert result["success"] is True
        inp_path = Path(result["input_file"])
        content = inp_path.read_text()
        assert "CPCM(water)" in content

    def test_opt_task(self, tmp_dir):
        """Test ORCA setup for optimization task."""
        xyz_file = tmp_dir / "mol.xyz"
        xyz_file.write_text("2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n")

        result = orca_setup(str(xyz_file), task="OPT")
        assert result["success"] is True
        inp_path = Path(result["input_file"])
        content = inp_path.read_text()
        assert "OPT" in content

    def test_missing_input_file(self):
        """Test that missing input file returns error."""
        result = orca_setup("/nonexistent.xyz")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_output_dir_created(self, tmp_dir):
        """Test that output directory is created if it does not exist."""
        xyz_file = tmp_dir / "mol.xyz"
        xyz_file.write_text("1\n\nH 0.0 0.0 0.0\n")
        out_dir = tmp_dir / "orca_output"

        result = orca_setup(str(xyz_file), output_dir=str(out_dir))
        assert result["success"] is True
        assert out_dir.exists()


# ── ORCA Parse ───────────────────────────────────────────────────────────────


class TestOrcaParse:
    def test_parse_mock_output(self, tmp_dir):
        """Test parsing a mock ORCA output file."""
        out_file = tmp_dir / "molecule.out"
        out_file.write_text(
            "FINAL SINGLE POINT ENERGY     -76.123456\n"
            "\n"
            "HOMO - LUMO gap                0.234567 a.u.\n"
            "\n"
            "THE OPTIMIZATION HAS CONVERGED\n"
            "\n"
            "SCF CONVERGED after  12 iterations\n"
        )

        result = orca_parse(str(out_file))

        assert result["success"] is True
        assert abs(result["energy_hartree"] - (-76.123456)) < 1e-5
        assert abs(result["energy_ev"] - (-76.123456 * 27.2114)) < 1e-3
        assert result["homo_lumo_gap"] == 0.234567
        assert result["geometry_converged"] is True
        assert result["scf_converged"] is True

    def test_parse_missing_file(self):
        """Test parsing a non-existent file."""
        result = orca_parse("/nonexistent.out")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_parse_no_convergence(self, tmp_dir):
        """Test parsing output without convergence."""
        out_file = tmp_dir / "mol.out"
        out_file.write_text(
            "FINAL SINGLE POINT ENERGY     -50.0\n"
            "SCF NOT CONVERGED\n"
        )

        result = orca_parse(str(out_file))
        assert result["success"] is True
        assert result["geometry_converged"] is False
        assert result["scf_converged"] is False


# ── Gaussian Setup ───────────────────────────────────────────────────────────


class TestGaussianSetup:
    def test_generates_valid_com_file(self, tmp_dir):
        """Test that gaussian_setup generates a valid .com file."""
        xyz_file = tmp_dir / "molecule.xyz"
        xyz_file.write_text("2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n")

        result = gaussian_setup(
            str(xyz_file),
            method="B3LYP",
            basis="6-31G*",
            charge=0,
            multiplicity=1,
            task="SP",
        )

        assert result["success"] is True
        assert "input_file" in result
        assert result["method"] == "B3LYP"
        assert result["basis"] == "6-31G*"

        com_path = Path(result["input_file"])
        assert com_path.exists()
        content = com_path.read_text()
        assert "B3LYP/6-31G*" in content
        assert "0 1" in content
        assert "%chk=" in content

    def test_opt_freq_task(self, tmp_dir):
        """Test Gaussian setup for OPT FREQ task."""
        xyz_file = tmp_dir / "mol.xyz"
        xyz_file.write_text("2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n")

        result = gaussian_setup(str(xyz_file), task="OPTFREQ")
        assert result["success"] is True
        com_path = Path(result["input_file"])
        content = com_path.read_text()
        assert "OPT FREQ" in content

    def test_missing_input_file(self):
        """Test that missing input file returns error."""
        result = gaussian_setup("/nonexistent.xyz")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_charged_molecule(self, tmp_dir):
        """Test setup with non-zero charge."""
        xyz_file = tmp_dir / "ion.xyz"
        xyz_file.write_text("1\n\nNa 0.0 0.0 0.0\n")

        result = gaussian_setup(str(xyz_file), charge=1, multiplicity=1)
        assert result["success"] is True
        com_path = Path(result["input_file"])
        content = com_path.read_text()
        assert "1 1" in content


# ── Gaussian Parse ───────────────────────────────────────────────────────────


class TestGaussianParse:
    def test_parse_mock_output(self, tmp_dir):
        """Test parsing a mock Gaussian log file."""
        log_file = tmp_dir / "molecule.log"
        log_file.write_text(
            "SCF Done:  E(RB3LYP) =  -76.1234567890     A.U. after   12 cycles\n"
            "\n"
            " Alpha  occ. eigenvalues --   -10.00000  -0.50000\n"
            " Alpha virt. eigenvalues --    0.10000    2.00000\n"
            "\n"
            " Normal termination of Gaussian 16\n"
        )

        result = gaussian_parse(str(log_file))

        assert result["success"] is True
        assert abs(result["energy_hartree"] - (-76.123456789)) < 1e-5
        assert result["normal_termination"] is True
        assert result["homo"] == -0.5
        assert result["lumo"] == 0.1
        assert abs(result["homo_lumo_gap"] - 0.6) < 1e-5

    def test_parse_with_frequencies(self, tmp_dir):
        """Test parsing Gaussian output with frequencies."""
        log_file = tmp_dir / "freq.log"
        log_file.write_text(
            "SCF Done:  E(RB3LYP) =  -76.0     A.U. after    8 cycles\n"
            "Frequencies --   100.5000   200.3000   3500.0000\n"
            " Normal termination of Gaussian 16\n"
        )

        result = gaussian_parse(str(log_file))
        assert result["success"] is True
        assert "frequencies" in result
        assert len(result["frequencies"]) == 3
        assert result["num_imaginary"] == 0

    def test_parse_with_imaginary_freq(self, tmp_dir):
        """Test parsing output with imaginary (negative) frequencies."""
        log_file = tmp_dir / "ts.log"
        log_file.write_text(
            "SCF Done:  E(RB3LYP) =  -76.0     A.U.\n"
            "Frequencies --   -500.0000   100.0000   200.0000\n"
        )

        result = gaussian_parse(str(log_file))
        assert result["success"] is True
        assert result["num_imaginary"] == 1
        assert len(result["imaginary_frequencies"]) == 1

    def test_parse_missing_file(self):
        """Test parsing a non-existent file."""
        result = gaussian_parse("/nonexistent.log")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_parse_optimization_converged(self, tmp_dir):
        """Test parsing output with optimization convergence."""
        log_file = tmp_dir / "opt.log"
        log_file.write_text(
            "SCF Done:  E(RB3LYP) =  -76.0     A.U.\n"
            "Optimized Parameters\n"
            " Normal termination of Gaussian 16\n"
        )

        result = gaussian_parse(str(log_file))
        assert result["optimization_converged"] is True

    def test_parse_thermal_corrections(self, tmp_dir):
        """Test parsing thermal corrections."""
        log_file = tmp_dir / "thermal.log"
        log_file.write_text(
            "Zero-point correction=                           0.045678\n"
            "Thermal correction to Energy=                    0.050000\n"
        )

        result = gaussian_parse(str(log_file))
        assert abs(result["zero_point_correction"] - 0.045678) < 1e-5
        assert abs(result["thermal_correction_energy"] - 0.05) < 1e-5


# ── Job Submission ───────────────────────────────────────────────────────────


class TestJobSubmission:
    def test_missing_working_dir(self):
        """Test that missing working directory returns error."""
        result = submit_job("echo hello", "/nonexistent/dir")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_unknown_scheduler(self, tmp_dir):
        """Test that unknown scheduler returns error."""
        result = submit_job("echo hello", str(tmp_dir), scheduler="unknown")
        assert result["success"] is False
        assert "Unknown scheduler" in result["error"]

    def test_slurm_missing_binary(self, tmp_dir):
        """Test that missing sbatch is detected."""
        result = submit_job("echo hello", str(tmp_dir), scheduler="slurm")
        # sbatch not found on most dev systems
        assert result["success"] is False
        assert "not found" in result["error"].lower() or "sbatch" in result.get("error", "").lower()

    def test_pbs_missing_binary(self, tmp_dir):
        """Test that missing qsub is detected."""
        result = submit_job("echo hello", str(tmp_dir), scheduler="pbs")
        assert result["success"] is False

    def test_local_submission(self, tmp_dir):
        """Test local job submission."""
        result = submit_job("echo hello", str(tmp_dir), scheduler="local")
        assert result["success"] is True
        assert "job_id" in result
        assert result["scheduler"] == "local"
        assert result["job_id"].startswith("local_")

    def test_check_local_job(self, tmp_dir):
        """Test checking a local job status."""
        result = submit_job("sleep 5", str(tmp_dir), scheduler="local")
        assert result["success"] is True

        check = check_job(result["job_id"], scheduler="local")
        assert check["success"] is True
        assert check["scheduler"] == "local"

    def test_cancel_local_job(self, tmp_dir):
        """Test cancelling a local job."""
        result = submit_job("sleep 60", str(tmp_dir), scheduler="local")
        assert result["success"] is True

        cancel = cancel_job(result["job_id"], scheduler="local")
        assert cancel["success"] is True

    def test_cancel_invalid_job_id(self):
        """Test cancelling with invalid job ID format."""
        result = cancel_job("invalid_id", scheduler="local")
        assert result["success"] is False

    def test_check_invalid_job_id(self):
        """Test checking with invalid job ID format."""
        result = check_job("invalid_id", scheduler="local")
        assert result["success"] is False

    def test_slurm_script_created(self, tmp_dir):
        """Test that a Slurm submit script is created even if sbatch fails."""
        result = submit_job("echo hello", str(tmp_dir), scheduler="slurm", job_name="test")
        # Script should be created even if sbatch is not available
        script = tmp_dir / "submit_slurm.sh"
        assert script.exists()
        content = script.read_text()
        assert "#SBATCH" in content
        assert "--job-name=test" in content


# ── QM Gates ─────────────────────────────────────────────────────────────────


class TestQmInputsDefined:
    def test_pass_with_xyz_and_inp(self, tmp_dir):
        """Test gate passes with valid coordinate and input files."""
        (tmp_dir / "mol.xyz").write_text("2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n")
        (tmp_dir / "mol.inp").write_text("! B3LYP def2-SVP\n* xyzfile 0 1 mol.xyz\n")

        result = qm_inputs_defined(str(tmp_dir))
        assert result["passed"] is True

    def test_fail_no_coordinates(self, tmp_dir):
        """Test gate fails with no coordinate files."""
        result = qm_inputs_defined(str(tmp_dir))
        assert result["passed"] is False
        assert "No coordinate files" in result["details"]["error"]

    def test_fail_invalid_xyz(self, tmp_dir):
        """Test gate fails with an invalid XYZ file."""
        (tmp_dir / "mol.xyz").write_text("invalid content\n")

        result = qm_inputs_defined(str(tmp_dir))
        assert result["passed"] is False

    def test_pass_with_gaussian_com(self, tmp_dir):
        """Test gate passes with a Gaussian .com file."""
        (tmp_dir / "mol.xyz").write_text("2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n")
        (tmp_dir / "mol.com").write_text(
            "# B3LYP/6-31G* SP\n\nTitle\n\n0 1\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n\n"
        )

        result = qm_inputs_defined(str(tmp_dir))
        assert result["passed"] is True

    def test_valid_xyz_content(self, tmp_dir):
        """Test that a properly formatted XYZ file validates."""
        (tmp_dir / "water.xyz").write_text(
            "3\nWater molecule\nO  0.0000  0.0000  0.0000\nH  0.7570  0.5870  0.0000\nH -0.7570  0.5870  0.0000\n"
        )

        result = qm_inputs_defined(str(tmp_dir))
        # Should fail on charge/multiplicity not being defined (no .inp/.com)
        assert result["passed"] is False
        # But coordinates should be valid
        assert result["details"]["water.xyz_valid"] is True
        assert result["details"]["water.xyz_natoms"] == 3


class TestScfConverged:
    def test_pass_with_orca_output(self, tmp_dir):
        """Test gate passes with converged ORCA output."""
        (tmp_dir / "mol.out").write_text(
            "FINAL SINGLE POINT ENERGY     -76.0\nSCF CONVERGED after  12 iterations\n"
        )

        result = scf_converged(str(tmp_dir))
        assert result["passed"] is True

    def test_pass_with_gaussian_output(self, tmp_dir):
        """Test gate passes with normal Gaussian termination."""
        (tmp_dir / "mol.log").write_text(
            "SCF Done:  E(RB3LYP) =  -76.0\nNormal termination of Gaussian 16\n"
        )

        result = scf_converged(str(tmp_dir))
        assert result["passed"] is True

    def test_fail_no_output_files(self, tmp_dir):
        """Test gate fails with no output files."""
        result = scf_converged(str(tmp_dir))
        assert result["passed"] is False
        assert "No QM output files" in result["details"]["error"]

    def test_fail_scf_not_converged(self, tmp_dir):
        """Test gate fails when SCF did not converge."""
        (tmp_dir / "mol.out").write_text("SCF NOT CONVERGED\n")

        result = scf_converged(str(tmp_dir))
        assert result["passed"] is False

    def test_multiple_outputs(self, tmp_dir):
        """Test gate with multiple output files, at least one converged."""
        (tmp_dir / "mol1.out").write_text("SCF NOT CONVERGED\n")
        (tmp_dir / "mol2.out").write_text("SCF CONVERGED after 8 iterations\n")

        result = scf_converged(str(tmp_dir))
        assert result["passed"] is True


# ── Server Registration Test ─────────────────────────────────────────────────


class TestPhase5ServerRegistration:
    def test_phase5_tools_registered(self):
        import asyncio
        from compchem_tools.server import mcp

        tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in [
            "orca_setup",
            "orca_run",
            "orca_parse",
            "gaussian_setup",
            "gaussian_run",
            "gaussian_parse",
            "submit_job",
            "check_job",
            "cancel_job",
        ]:
            assert name in tool_names, f"Missing Phase 5 tool: {name}"

    def test_phase5_gates_registered(self):
        from compchem_tools.gates import GATE_REGISTRY

        for name in ["qm_inputs_defined", "scf_converged"]:
            assert name in GATE_REGISTRY, f"Missing gate: {name}"

    def test_phase5_tools_return_json(self, tmp_dir):
        """Test that Phase 5 server wrappers return valid JSON strings."""
        import asyncio
        from compchem_tools.server import (
            orca_parse,
            gaussian_parse,
            check_job,
            cancel_job,
        )

        # These should return JSON strings
        result = orca_parse("/nonexistent.out")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert "success" in parsed

        result = gaussian_parse("/nonexistent.log")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert "success" in parsed

        result = check_job("local_99999_abc", scheduler="local")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert "success" in parsed
