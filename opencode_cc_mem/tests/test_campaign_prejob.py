import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.prejob import PrejobError, validate_prejob  # noqa: E402


def pdb_line(chain, residue):
    return (
        f"ATOM      1  CA  ALA {chain}{residue:4d}      "
        "0.000   0.000   0.000  1.00  0.00           C\n"
    )


def make_haddock_dir(tmp_path):
    directory = tmp_path / "job"
    directory.mkdir()
    (directory / "receptor.pdb").write_text(pdb_line("A", 1))
    (directory / "peptide.pdb").write_text(pdb_line("B", 2))
    (directory / "receptor.actpass").write_text("1\n1\n")
    (directory / "peptide.actpass").write_text("2\n2\n")
    (directory / "ambig.tbl").write_text(
        "assign (resi 1 and segid A) (resi 2 and segid B) 2.0 2.0 0.0\n"
    )
    (directory / "config.cfg").write_text(
        "\n".join(
            [
                'run_dir = "output"',
                'molecules = ["receptor.pdb", "peptide.pdb"]',
                "ncores = 2",
                "[rigidbody]",
                'ambig_fname = "ambig.tbl"',
                "[flexref]",
                'ambig_fname = "ambig.tbl"',
                "fle_sta_2 = 2",
                "fle_end_2 = 2",
                'fle_seg_2 = "B"',
            ]
        )
    )
    return directory


def test_valid_haddock_inputs_pass(tmp_path):
    directory = make_haddock_dir(tmp_path)
    assert validate_prejob(directory, "haddock3") == []


def test_missing_chain_id_is_rejected(tmp_path):
    directory = make_haddock_dir(tmp_path)
    (directory / "peptide.pdb").write_text(
        "ATOM      1  CA  ALA   2       0.000   0.000   0.000\n"
    )
    with pytest.raises(PrejobError, match="missing chain ID"):
        validate_prejob(directory, "haddock3")


def test_empty_input_is_rejected(tmp_path):
    directory = make_haddock_dir(tmp_path)
    (directory / "empty.txt").write_text("")
    with pytest.raises(PrejobError, match="empty input"):
        validate_prejob(directory, "haddock3")


def test_symlink_is_rejected(tmp_path):
    directory = make_haddock_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (directory / "link.txt").symlink_to(outside)
    with pytest.raises(PrejobError, match="symlink"):
        validate_prejob(directory, "haddock3")


def test_ambig_residues_must_exist_in_actpass(tmp_path):
    directory = make_haddock_dir(tmp_path)
    (directory / "ambig.tbl").write_text(
        "assign (resi 1 and segid A) (resi 99 and segid B) 2.0 2.0 0.0\n"
    )
    with pytest.raises(PrejobError, match="outside actpass"):
        validate_prejob(directory, "haddock3")


def test_fle_range_must_match_first_and_last_residue(tmp_path):
    directory = make_haddock_dir(tmp_path)
    (directory / "peptide.pdb").write_text(
        "\n".join(pdb_line("B", residue) for residue in range(1, 7))
    )
    config = (directory / "config.cfg").read_text().replace(
        "fle_sta_2 = 2", "fle_sta_2 = 1"
    )
    (directory / "config.cfg").write_text(config)
    with pytest.raises(PrejobError, match="fle_end_2 2 does not match"):
        validate_prejob(directory, "haddock3")


def test_missing_tool_with_haddock_config_is_rejected(tmp_path):
    directory = make_haddock_dir(tmp_path)
    with pytest.raises(PrejobError, match="tool is required"):
        validate_prejob(directory, "")


def test_unknown_tool_with_haddock_config_is_rejected(tmp_path):
    directory = make_haddock_dir(tmp_path)
    with pytest.raises(PrejobError, match="requires tool haddock3"):
        validate_prejob(directory, "gromacs")


def test_actpass_requires_two_lines(tmp_path):
    directory = make_haddock_dir(tmp_path)
    (directory / "peptide.actpass").write_text("2\n")
    with pytest.raises(PrejobError, match="exactly two"):
        validate_prejob(directory, "haddock3")


def test_invalid_sdf_is_rejected(tmp_path):
    directory = make_haddock_dir(tmp_path)
    (directory / "ligand.sdf").write_text("bad\n")
    with pytest.raises(PrejobError, match="truncated header"):
        validate_prejob(directory, "haddock3")


def test_fle_range_must_exist_in_peptide_pdb(tmp_path):
    directory = make_haddock_dir(tmp_path)
    config = (directory / "config.cfg").read_text().replace(
        "fle_end_2 = 2", "fle_end_2 = 9"
    )
    (directory / "config.cfg").write_text(config)
    with pytest.raises(PrejobError, match="fle_end_2 9"):
        validate_prejob(directory, "haddock3")


def test_existing_output_is_rejected(tmp_path):
    directory = make_haddock_dir(tmp_path)
    (directory / "output").mkdir()
    with pytest.raises(PrejobError, match="output directory"):
        validate_prejob(directory, "haddock3")
