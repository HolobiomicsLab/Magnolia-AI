"""The generated job.slurm must stamp $SLURM_JOB_ID into .magnolia/jobid
at runtime, so the cluster-side record is self-describing."""
from pathlib import Path
from compchem_tools.tools.ssh_slurm import _write_sbatch_script


def test_sbatch_stamps_jobid(tmp_path):
    p = _write_sbatch_script(
        tmp_path,
        job_name="t",
        account="a",
        qos="q",
        partition="p",
        time_limit="00:10:00",
        ncores=1,
        memory="1GB",
        modulefiles_use="$HOME/modulefiles",
        tool="xtb",
        command="xtb input.xyz",
    )
    body = Path(p).read_text()
    # Stamp the slurm jobid into .magnolia/jobid in the run dir
    assert "mkdir -p .magnolia" in body
    assert 'echo "$SLURM_JOB_ID" > .magnolia/jobid' in body
    # Stamp must come AFTER cd "$SLURM_SUBMIT_DIR" so .magnolia is in the run dir
    cd_idx = body.index('cd "$SLURM_SUBMIT_DIR"')
    stamp_idx = body.index('echo "$SLURM_JOB_ID" > .magnolia/jobid')
    assert stamp_idx > cd_idx
    # And before the user's command
    cmd_idx = body.index("xtb input.xyz")
    assert stamp_idx < cmd_idx
