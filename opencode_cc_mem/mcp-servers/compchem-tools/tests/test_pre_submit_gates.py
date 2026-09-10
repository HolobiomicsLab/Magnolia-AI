"""Tests for per-tool mechanical pre-submit gates (execution plan P0.3).

Test-of-record:
- gates/structure.py::pdb_files_have_chain_ids -> test_pdb_files_gate_*
- tools/jobs.py::_check_pre_submit_gates -> test_pre_submit_gate_*
- tools/jobs.py::submit_job wiring -> test_submit_job_*
"""

from compchem_tools.gates import GATE_REGISTRY
from compchem_tools.gates.structure import pdb_files_have_chain_ids
from compchem_tools.tools.jobs import _check_pre_submit_gates, submit_job

# line[21] is the chain-ID column (0-based), per PDB spec.
GOOD_PDB = (
    "ATOM      1  N   ALA A   1      11.104   6.134  -6.504  1.00 20.00           N\n"
    "END\n"
)
BAD_PDB = GOOD_PDB.replace("ALA A", "ALA  ", 1)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_gate_registered():
    assert "pdb_files_have_chain_ids" in GATE_REGISTRY


def test_pdb_files_gate_passes_with_good_pdb(tmp_path):
    _write(tmp_path, "good.pdb", GOOD_PDB)
    r = pdb_files_have_chain_ids(str(tmp_path))
    assert r["passed"] is True
    assert len(r["files"]) == 1 and r["files"][0]["chain_ids"] == ["A"]


def test_pdb_files_gate_fails_with_bad_pdb(tmp_path):
    _write(tmp_path, "good.pdb", GOOD_PDB)
    _write(tmp_path, "bad.pdb", BAD_PDB)
    r = pdb_files_have_chain_ids(str(tmp_path))
    assert r["passed"] is False
    bad = [f for f in r["files"] if not f["passed"]]
    assert len(bad) == 1 and bad[0]["path"].endswith("bad.pdb")


def test_pdb_files_gate_no_files_passes_with_note(tmp_path):
    r = pdb_files_have_chain_ids(str(tmp_path))
    assert r["passed"] is True and r["files"] == [] and "note" in r


def test_pre_submit_gate_blocks_haddock3_with_bad_pdb(tmp_path):
    _write(tmp_path, "peptide.pdb", BAD_PDB)
    hold = _check_pre_submit_gates("haddock3", str(tmp_path), acknowledge=False)
    assert hold is not None
    assert hold["success"] is False
    assert hold["gate"]["name"] == "pdb_files_have_chain_ids"
    assert "hint" in hold


def test_pre_submit_gate_skips_other_tools(tmp_path):
    _write(tmp_path, "peptide.pdb", BAD_PDB)
    assert _check_pre_submit_gates("gnina", str(tmp_path), acknowledge=False) is None
    assert _check_pre_submit_gates(None, str(tmp_path), acknowledge=False) is None


def test_submit_job_blocks_haddock3_with_bad_pdb(tmp_path):
    _write(tmp_path, "peptide.pdb", BAD_PDB)
    res = submit_job("haddock3 run.cfg", str(tmp_path), scheduler="local",
                     tool="haddock3")
    assert res["success"] is False
    assert res["gate"]["name"] == "pdb_files_have_chain_ids"
    assert "job_id" not in res  # nothing was launched


def test_submit_job_acknowledge_overrides_gate(tmp_path):
    _write(tmp_path, "peptide.pdb", BAD_PDB)
    res = submit_job("true", str(tmp_path), scheduler="local", tool="haddock3",
                     acknowledge=True)
    assert res["success"] is True and "job_id" in res


def test_submit_job_passes_with_good_pdb(tmp_path):
    _write(tmp_path, "peptide.pdb", GOOD_PDB)
    res = submit_job("true", str(tmp_path), scheduler="local", tool="haddock3")
    assert res["success"] is True and "job_id" in res
