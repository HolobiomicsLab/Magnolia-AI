"""run_dir_self_contained gate: fail-closed on symlinks escaping the run dir
(prejob_check rule #3), plus the wiring-drift guard (gate not wired = test
fails, not warns)."""
from pathlib import Path

from compchem_tools.gates import GATE_REGISTRY
from compchem_tools.gates.hygiene import run_dir_self_contained
from compchem_tools.tools.jobs import _PRE_SUBMIT_GATES, submit_job


def test_clean_dir_passes(tmp_path):
    (tmp_path / "input.pdb").write_text("ATOM\n")
    assert run_dir_self_contained(str(tmp_path))["passed"] is True


def test_internal_symlink_passes(tmp_path):
    real = tmp_path / "real.pdb"
    real.write_text("ATOM\n")
    (tmp_path / "link.pdb").symlink_to(real)
    assert run_dir_self_contained(str(tmp_path))["passed"] is True


def test_external_symlink_fails_and_names_it(tmp_path, monkeypatch):
    outside = tmp_path.parent / "secret.pdb"
    outside.write_text("ATOM\n")
    (tmp_path / "input.pdb").symlink_to(outside)
    result = run_dir_self_contained(str(tmp_path))
    assert result["passed"] is False
    assert any("outside run dir" in v for v in result["violations"])


def test_broken_symlink_fails_closed(tmp_path):
    (tmp_path / "dangling.pdb").symlink_to(tmp_path / "nowhere.pdb")
    result = run_dir_self_contained(str(tmp_path))
    assert result["passed"] is False
    assert any("broken" in v for v in result["violations"])


def test_missing_dir_fails_closed(tmp_path):
    result = run_dir_self_contained(str(tmp_path / "nope"))
    assert result["passed"] is False
    assert "error" in result


def test_gate_registered():
    assert GATE_REGISTRY["run_dir_self_contained"] is run_dir_self_contained


def test_pre_submit_gate_wiring_not_drifted():
    """Every gate named in _PRE_SUBMIT_GATES must exist in the registry —
    a wiring typo must fail tests, not silently skip the gate."""
    for tool, gates in _PRE_SUBMIT_GATES.items():
        for name in gates:
            assert name in GATE_REGISTRY, f"{tool}: gate '{name}' not registered"


def _chain_ok_workdir(tmp_path):
    (tmp_path / "input.pdb").write_text(
        "ATOM      1  N   ALA A   1      11.104  13.207   9.447  1.00 20.00\n"
    )


def test_submit_job_blocked_by_external_symlink(tmp_path, monkeypatch):
    import compchem_tools.tools.jobs as jobs_mod
    from compchem_memory.tiers.project import ProjectManager
    monkeypatch.setattr(jobs_mod, "_PROJECT_MANAGER",
                        ProjectManager(tmp_path / ".magnolia"))
    _chain_ok_workdir(tmp_path)
    outside = tmp_path.parent / "outside.cfg"
    outside.write_text("# config\n")
    (tmp_path / "config.cfg").symlink_to(outside)

    out = submit_job("haddock3 config.cfg", str(tmp_path),
                     scheduler="local", tool="haddock3",
                     project_dir=str(tmp_path))
    assert out["success"] is False
    assert "run_dir_self_contained" in out["error"]
    # no run record may exist for a blocked submission
    runs = list((tmp_path / ".magnolia" / "runs").glob("*.yaml"))
    assert [r for r in runs if r.name != "INDEX.yaml"] == []


def test_submit_job_acknowledge_overrides_symlink_gate(tmp_path, monkeypatch):
    import compchem_tools.tools.jobs as jobs_mod
    from compchem_memory.tiers.project import ProjectManager
    monkeypatch.setattr(jobs_mod, "_PROJECT_MANAGER",
                        ProjectManager(tmp_path / ".magnolia"))
    _chain_ok_workdir(tmp_path)
    outside = tmp_path.parent / "outside2.cfg"
    outside.write_text("# config\n")
    (tmp_path / "config.cfg").symlink_to(outside)

    out = submit_job("true", str(tmp_path), scheduler="local", tool="haddock3",
                     project_dir=str(tmp_path), acknowledge=True)
    assert out["success"] is True
