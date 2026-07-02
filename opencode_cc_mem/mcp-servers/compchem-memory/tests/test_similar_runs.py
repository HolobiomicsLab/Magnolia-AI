import tempfile
from pathlib import Path
import pytest
import yaml
from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        pd = Path(d) / "project"
        pd.mkdir()
        yield pd


def test_record_run_persists_system_tags(project_dir):
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    path = mgr.record_run(pd, run_id="r1", tool="haddock3", status="pass",
                          system_tags=["peptide", "6mer"])
    rec = yaml.safe_load(Path(path).read_text())
    assert rec["system_tags"] == ["peptide", "6mer"]


def test_record_run_system_tags_defaults_empty(project_dir):
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    path = mgr.record_run(pd, run_id="r2", tool="haddock3", status="pass")
    rec = yaml.safe_load(Path(path).read_text())
    assert rec["system_tags"] == []
