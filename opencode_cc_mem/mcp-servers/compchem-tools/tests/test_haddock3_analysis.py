# tests/test_haddock3_analysis.py
"""HADDOCK3 analyzer: capri metrics plus contact/per-residue contributions."""
from pathlib import Path

from compchem_tools.analysis.haddock3 import analyze
from compchem_tools.analysis.assessor import assess_run

CONTACTS = """################################################################################
# comment line
res1\tres2\tca-ca-cont-probability\tca-ca-dist\tcontact-type\tshortest-cont-probability\tshortest-dist
A-80-ASP\tB-13-PHE\t0.85\t8.5\tnegative-apolar\t0.75\t4.7
A-81-ALA\tB-13-PHE\t0.9\t6.9\tapolar-apolar\t0.85\t3.9
A-81-ALA\tB-14-GLU\t0.4\t6.8\tapolar-negative\t0.8\t4.4
B-1-LYS\tA-80-ASP\t0.6\t5.0\tapolar-negative\t0.5\t3.0
"""


def _run_dir(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "output" / "13_contactmap").mkdir(parents=True)
    (run_dir / "output" / "13_contactmap" / "cluster1_interchain_contacts.tsv").write_text(CONTACTS)
    return run_dir


def test_contact_analysis_counts_and_hotspots(tmp_path):
    run_dir = _run_dir(tmp_path)
    result = analyze(run_dir)
    contacts = result["metrics"]["contacts"]

    assert contacts["clusters"]["cluster1"]["n_contacts"] == 3
    assert contacts["receptor_hotspots"][0]["residue"] == "A-80-ASP"
    assert contacts["receptor_hotspots"][0]["score"] == 1.45
    assert contacts["ligand_hotspots"][0]["residue"] == "B-13-PHE"
    assert contacts["ligand_hotspots"][0]["score"] == 1.75


def test_assess_run_includes_contacts_for_haddock3(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "output" / "01_caprieval").mkdir()
    (run_dir / "output" / "01_caprieval" / "io.json").write_text("{}")
    (run_dir / "output" / "02_caprieval").mkdir()
    (run_dir / "output" / "02_caprieval" / "capri_ss.tsv").write_text("a\tb\n")
    (run_dir / "output" / "03_clustfcc").mkdir()
    (run_dir / "output" / "03_clustfcc" / "clustfcc.txt").write_text("cluster 1\n")

    result = assess_run(str(run_dir), "haddock3", exit_code=0)

    assert result["overall"] in ("pass", "warning")
    assert "contacts" in result["metrics"]
    assert result["metrics"]["contacts"]["clusters"]["cluster1"]["n_contacts"] == 3


def test_no_contact_files_returns_no_contacts(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = analyze(run_dir)
    assert "contacts" not in result["metrics"]


def test_atom_level_keys_aggregate_to_residue(tmp_path):
    run_dir = _run_dir(tmp_path)
    extra = (
        "A-80-ASP-OD1\tB-13-PHE-CD1\t0.5\t4.0\tnegative-apolar\t0.5\t3.0\n"
        "A-80-ASP-OD2\tB-13-PHE-CD2\t0.5\t4.1\tnegative-apolar\t0.5\t3.1\n"
    )
    path = run_dir / "output" / "13_contactmap" / "cluster1_interchain_contacts.tsv"
    path.write_text(path.read_text() + extra)

    result = analyze(run_dir)
    hotspots = result["metrics"]["contacts"]["receptor_hotspots"]
    assert hotspots[0]["residue"] == "A-80-ASP"
    assert hotspots[0]["score"] == 2.45  # 1.45 from residue rows + 1.0 from atom rows
