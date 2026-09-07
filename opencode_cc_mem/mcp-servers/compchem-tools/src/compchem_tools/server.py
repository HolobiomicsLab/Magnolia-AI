"""compchem-tools MCP server: Domain tools for computational chemistry."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "compchem-memory" / "src"))
from compchem_memory.capture import captured

from fastmcp import FastMCP

from compchem_tools.tools.haddock3 import (
    haddock3_run as _haddock3_run,
    haddock3_parse_results as _haddock3_parse,
    generate_restraints as _generate_restraints,
    run_acpype as _run_acpype,
)
from compchem_tools.tools.preprocess import (
    preprocess_pdb as _preprocess_pdb,
    validate_structure as _validate_structure,
)
from compchem_tools.tools.environment import (
    check_environment as _check_environment,
)
from compchem_tools.tools.gnina import (
    gnina_dock as _gnina_dock,
    gnina_parse_results as _gnina_parse,
)
from compchem_tools.tools.covalent import (
    smarts_validate as _smarts_validate,
    alkyne_to_vinyl as _alkyne_to_vinyl,
)
from compchem_tools.tools.xtb import (
    xtb_optimize as _xtb_optimize,
    xtb_singlepoint as _xtb_singlepoint,
)
from compchem_tools.tools.orca import (
    orca_setup as _orca_setup,
    orca_run as _orca_run,
    orca_parse as _orca_parse,
)
from compchem_tools.tools.gaussian import (
    gaussian_setup as _gaussian_setup,
    gaussian_run as _gaussian_run,
    gaussian_parse as _gaussian_parse,
)
from compchem_tools.tools.jobs import (
    submit_job as _submit_job,
    check_job as _check_job,
    cancel_job as _cancel_job,
    check_run_status as _check_run_status,
)
from compchem_tools.tools.p2rank import (
    p2rank_predict as _p2rank_predict,
)
from compchem_tools.tools.gromacs import (
    gromacs_setup as _gromacs_setup,
    gromacs_run as _gromacs_run,
    gromacs_parse as _gromacs_parse,
)
from compchem_tools.tools.workflow import (
    load_workflow as _load_workflow,
    get_workflow_status as _get_workflow_status,
)
from compchem_tools.tools.shell import run_shell as _run_shell
from compchem_tools.gates import GATE_REGISTRY
from compchem_tools.progress import parse_haddock_progress

PROJECT_DIR = os.environ.get("MAGNOLIA_PROJECT_DIR", ".")

mcp = FastMCP("compchem-tools")

_active_sessions: dict[str, dict[str, Any]] = {}


# ── v1 Tools (preserved) ────────────────────────────────────────────────────


@mcp.tool()
@captured(source="compchem-tools")
def haddock3_run(
    config_path: str,
    run_dir: str | None = None,
    ncores: int = 40,
    mode: str = "local",
    restart_from: int | None = None,
) -> str:
    """Validate inputs, write config if needed, launch haddock3 in the run directory.
    Returns session_id for status polling.

    Call this when: running a HADDOCK3 protein-protein or protein-ligand docking job."""
    result = _haddock3_run(config_path, run_dir, ncores, mode, restart_from)
    if result.get("success"):
        session_id = _generate_session_id()
        _active_sessions[session_id] = {
            "session_id": session_id,
            "run_dir": result["run_dir"],
            "pid": result["pid"],
            "tool": "haddock3",
            "status": "running",
            "started": datetime.now(timezone.utc).isoformat(),
        }
        result["session_id"] = session_id
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def haddock3_parse_results(run_dir: str) -> str:
    """Parse caprieval/clustfcc/seletopclusts outputs.
    Returns structured metrics (best score, cluster count, LRMSD range).

    Call this when: a HADDOCK3 run has completed and you need to extract scoring metrics."""
    result = _haddock3_parse(run_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def preprocess_pdb(
    input_path: str,
    output_path: str | None = None,
    add_chain_id: str | None = None,
    remove_waters: bool = True,
    fix_atom_names: bool = True,
) -> str:
    """Add chain IDs, fix atom names, remove waters. Returns path to cleaned PDB.

    Call this when: preparing a raw PDB structure for use in docking or MD simulations."""
    result = _preprocess_pdb(
        input_path, output_path, add_chain_id, remove_waters, fix_atom_names
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def generate_restraints(
    actpass_file_1: str,
    actpass_file_2: str,
    output_path: str,
    segid_one: str | None = None,
    segid_two: str | None = None,
) -> str:
    """From actpass files, run haddock3-restraints active_passive_to_ambig.
    Returns path to ambig.tbl.

    Call this when: converting active/passive residue definitions into HADDOCK3 ambiguous restraints."""
    result = _generate_restraints(
        actpass_file_1, actpass_file_2, output_path, segid_one, segid_two
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def run_acpype(
    input_file: str,
    charge_method: str = "bcc",
    output_dir: str | None = None,
) -> str:
    """Run ACPYPE for ligand parameterisation. Post-process atom types to uppercase.
    Returns paths to .top and .par files.

    Call this when: generating GAFF/AMBER force field parameters for a small molecule ligand."""
    result = _run_acpype(input_file, charge_method, output_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def validate_structure(
    input_path: str,
    expected_format: str | None = None,
) -> str:
    """Basic sanity checks on PDB/SDF: atom count, chain IDs present, non-zero
    size, parseable by a structure library.

    Call this when: verifying a structure file is well-formed before submitting it to a docking or simulation tool."""
    result = _validate_structure(input_path, expected_format)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def check_environment(
    tool_name: str,
    min_version: str | None = None,
    check_conda: bool = False,
) -> str:
    """Verify that a given tool binary is available, report version, check conda env.

    Call this when: confirming a required scientific tool is installed and accessible before running a job."""
    result = _check_environment(tool_name, min_version, check_conda)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def stage_gate(
    gate_name: str,
    working_directory: str,
) -> str:
    """Run a named gate check (e.g. 'docking_inputs_ready').
    Takes gate name + working directory. Returns pass/fail with details.

    Call this when: validating that all required inputs for a workflow stage are present before proceeding."""
    if gate_name not in GATE_REGISTRY:
        available = ", ".join(sorted(GATE_REGISTRY.keys()))
        return json.dumps(
            {"error": f"Unknown gate: {gate_name}", "available_gates": available},
            indent=2,
        )

    gate_fn = GATE_REGISTRY[gate_name]
    if gate_name in ("pdb_has_chain_id",):
        return json.dumps(gate_fn(working_directory), indent=2)

    result = gate_fn(working_directory)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def check_run_status(run_dir: str) -> str:
    """Check if a computation run has completed.

    Authoritative-first: for tracked ssh-slurm runs this reports from the run
    record's lifecycle/slurm state (so a job that COMPLETED on the cluster but
    isn't fetched yet reads as completed with results_local=False, NOT failed);
    purely-local runs fall back to inspecting output files.

    Call this when: polling whether a previously launched run directory has finished."""
    return json.dumps(_check_run_status(run_dir), indent=2)


# ── Phase 3: Gnina / Covalent / xTB Tools ───────────────────────────────────


@mcp.tool()
@captured(source="compchem-tools")
def gnina_dock(
    receptor: str,
    ligand: str,
    out_dir: str | None = None,
    center_x: float | None = None,
    center_y: float | None = None,
    center_z: float | None = None,
    size_x: float = 25.0,
    size_y: float = 25.0,
    size_z: float = 25.0,
    num_modes: int = 20,
    exhaustiveness: int = 8,
    covalent: bool = False,
    covalent_receptor_atom: str | None = None,
    covalent_ligand_atom_pattern: str | None = None,
) -> str:
    """Run Gnina molecular docking (classical or covalent mode).
    Returns paths to output poses and scores.

    Call this when: docking a small molecule into a protein binding pocket with Gnina (classical or covalent)."""
    result = _gnina_dock(
        receptor, ligand, out_dir, None,
        center_x, center_y, center_z,
        size_x, size_y, size_z,
        num_modes, exhaustiveness, None,
        covalent, covalent_receptor_atom, covalent_ligand_atom_pattern,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def gnina_parse_results(run_dir: str) -> str:
    """Parse Gnina docking output SDF for scores and pose information.

    Call this when: extracting CNNscore, CNNaffinity, and pose data from a Gnina docking output SDF."""
    result = _gnina_parse(run_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def smarts_validate(smarts: str, smiles: str | None = None) -> str:
    """Validate a SMARTS pattern. Optionally check it matches a SMILES molecule.
    Returns validity and match count.

    Call this when: verifying a SMARTS pattern is syntactically correct and matches the intended substructure."""
    result = _smarts_validate(smarts, smiles)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def alkyne_to_vinyl(alkyne_smiles: str, output_dir: str | None = None) -> str:
    """Convert an alkyne SMILES to Z and E vinyl isomers for covalent docking.
    Returns isomer SMILES strings.

    Call this when: preparing a covalent warhead alkyne for covalent docking by generating its vinyl isomers."""
    result = _alkyne_to_vinyl(alkyne_smiles, output_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def xtb_optimize(
    input_file: str,
    output_dir: str | None = None,
    method: str = "gfn2",
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
    ncores: int = 4,
) -> str:
    """Run xTB geometry optimization (GFN2-xTB). Returns optimized structure and energy.

    Call this when: performing a fast semi-empirical geometry optimization on a small molecule or fragment."""
    result = _xtb_optimize(input_file, output_dir, method, charge, uhf, solvent, ncores=ncores)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def xtb_singlepoint(
    input_file: str,
    output_dir: str | None = None,
    method: str = "gfn2",
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
    ncores: int = 4,
) -> str:
    """Run xTB single-point energy calculation. Returns energy and properties.

    Call this when: computing a fast single-point energy and electronic properties for a structure without geometry optimization."""
    result = _xtb_singlepoint(input_file, output_dir, method, charge, uhf, solvent, ncores=ncores)
    return json.dumps(result, indent=2)


# ── Phase 4: Workflow / P2Rank / GROMACS Tools ───────────────────────────────


@mcp.tool()
@captured(source="compchem-tools")
def workflow_load(template_path: str) -> str:
    """Load and validate a YAML workflow template. Returns the workflow plan.

    Call this when: loading a multi-step workflow definition before executing it."""
    result = _load_workflow(template_path)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def workflow_status(run_dir: str) -> str:
    """Check which steps in a workflow are complete based on output files.

    Call this when: monitoring progress of a multi-step workflow to determine which stages have finished."""
    result = _get_workflow_status(run_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def p2rank_predict(
    protein: str,
    output_dir: str | None = None,
    threads: int = 4,
) -> str:
    """Run P2Rank pocket prediction on a protein structure.
    Returns ranked pocket list with scores and residues.

    Call this when: identifying druggable binding pockets on a protein structure before docking."""
    result = _p2rank_predict(protein, output_dir, threads)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def gromacs_setup(
    structure: str,
    topology: str | None = None,
    forcefield: str = "amber99sb-ildn",
    water: str = "tip3p",
    box_type: str = "dodecahedron",
    box_distance: float = 1.0,
    output_dir: str | None = None,
) -> str:
    """Set up a GROMACS MD simulation: generate topology, box, solvate, ions.
    Returns paths to .gro, .top, and other setup files.

    Call this when: preparing a GROMACS MD simulation system from an initial structure."""
    result = _gromacs_setup(
        structure, topology, forcefield, water, box_type, box_distance, output_dir
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def gromacs_run(
    tpr_file: str,
    output_dir: str | None = None,
    ncores: int = 4,
) -> str:
    """Run a GROMACS MD simulation from a .tpr file.
    Returns paths to trajectory, energy, and log files.

    Call this when: executing a GROMACS MD simulation from a prepared .tpr input file."""
    result = _gromacs_run(tpr_file, output_dir, ncores)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def gromacs_parse(
    energy_file: str | None = None,
    trajectory: str | None = None,
) -> str:
    """Parse GROMACS output: extract energy terms from .edr and trajectory info.

    Call this when: extracting energy terms and trajectory statistics from a completed GROMACS simulation."""
    result = _gromacs_parse(energy_file, trajectory)
    return json.dumps(result, indent=2)


# ── Phase 5: ORCA / Gaussian / Job Management Tools ─────────────────────────


@mcp.tool()
@captured(source="compchem-tools")
def orca_setup(
    input_file: str,
    method: str = "B3LYP",
    basis: str = "def2-SVP",
    charge: int = 0,
    multiplicity: int = 1,
    task: str = "SP",
    solvent: str | None = None,
    output_dir: str | None = None,
    ncores: int = 4,
) -> str:
    """Generate ORCA input file from coordinates.
    Returns path to the generated .inp file.

    Call this when: preparing an ORCA quantum chemistry input file for a given structure and method."""
    result = _orca_setup(
        input_file, method, basis, charge, multiplicity, task, solvent, output_dir, ncores
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def orca_run(
    input_file: str,
    output_dir: str | None = None,
    ncores: int = 4,
) -> str:
    """Run ORCA calculation from an .inp file.
    Returns paths to output files.

    Call this when: executing an ORCA DFT or semi-empirical quantum chemistry calculation."""
    result = _orca_run(input_file, output_dir, ncores)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def orca_parse(output_file: str) -> str:
    """Parse ORCA output for energy, HOMO-LUMO gap, and converged geometry.

    Call this when: extracting energy, frontier orbital gaps, and geometry from a completed ORCA output file."""
    result = _orca_parse(output_file)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def gaussian_setup(
    input_file: str,
    method: str = "B3LYP",
    basis: str = "6-31G*",
    charge: int = 0,
    multiplicity: int = 1,
    task: str = "SP",
    output_dir: str | None = None,
) -> str:
    """Generate Gaussian .com input file from coordinates.
    Returns path to the generated .com file.

    Call this when: preparing a Gaussian input file for DFT or ab initio quantum chemistry calculation."""
    result = _gaussian_setup(input_file, method, basis, charge, multiplicity, task, output_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def gaussian_run(
    input_file: str,
    output_dir: str | None = None,
    ncores: int = 4,
) -> str:
    """Run Gaussian calculation from a .com file.
    Returns paths to output files.

    Call this when: executing a Gaussian quantum chemistry calculation from a prepared .com file."""
    result = _gaussian_run(input_file, output_dir, ncores)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def gaussian_parse(output_file: str) -> str:
    """Parse Gaussian .log output for energy, HOMO-LUMO, and frequencies.

    Call this when: extracting energy, orbital gaps, and vibrational frequencies from a completed Gaussian log."""
    result = _gaussian_parse(output_file)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def submit_job(
    command: str,
    working_dir: str,
    scheduler: str = "slurm",
    job_name: str = "compchem",
    ncores: int = 4,
    memory: str = "4GB",
    time_limit: str = "24:00:00",
    partition: str | None = None,
    # ssh-slurm-specific kwargs
    project_dir: str | None = None,
    cluster: str | None = None,
    account: str | None = None,
    qos: str | None = None,
    tool: str | None = None,
    # restart/resume an existing run in its remote dir (ssh-slurm only)
    restart_of: str | None = None,
    remote_precommand: str | None = None,
    acknowledge: bool = False,
    system_tags: list[str] | None = None,
) -> str:
    """Submit a job to Slurm, PBS, ssh-slurm, or run locally.
    Returns job ID and submission details.

    Call this when: submitting a long-running (>30 min) computation to a job
    scheduler instead of running it in the foreground.

    cluster is optional: left unset it resolves from the configured site
    profiles ($MAGNOLIA_CLUSTER, the profile marked default, or the only one
    configured). Pass it only when several clusters are configured.

    To RESTART/resume an ssh-slurm run in place (e.g. haddock3 --restart N,
    gromacs -cpi): pass restart_of=<prior run_id> and the resume command; it
    reuses that run's remote dir (preserving partial output) and run record
    instead of starting fresh. Use remote_precommand to clean an incomplete
    step first (e.g. "rm -rf output/04_flexref").
    acknowledge: set True to proceed past a recall-gate hold after reviewing the
    surfaced pitfalls (resubmit with any parameter changes you decided on).
    system_tags: short descriptors of the system (e.g. ["peptide","6mer","hsc70"])
      used to recall the most similar past run before launch."""
    result = _submit_job(
        command, working_dir, scheduler, job_name, ncores, memory, time_limit, partition,
        project_dir=project_dir, cluster=cluster, account=account, qos=qos, tool=tool,
        restart_of=restart_of, remote_precommand=remote_precommand, acknowledge=acknowledge,
        system_tags=system_tags,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def check_job(
    job_id: str,
    scheduler: str = "slurm",
    # ssh-slurm-specific kwargs
    cluster: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Check job status on Slurm, PBS, or local.
    Returns current status information.

    Call this when: monitoring the status of a previously submitted job by its job ID."""
    result = _check_job(job_id, scheduler, cluster=cluster, project_dir=project_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def cancel_job(
    job_id: str,
    scheduler: str = "slurm",
    # ssh-slurm-specific kwargs
    cluster: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Cancel a running job on Slurm, PBS, or local.
    Returns cancellation status.

    Call this when: aborting a running job that is no longer needed or has gone wrong."""
    result = _cancel_job(job_id, scheduler, cluster=cluster, project_dir=project_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def fetch_job_results(
    job_id: str,
    project_dir: str | None = None,
) -> str:
    """Pull a remote SSH-Slurm job's run dir back to its recorded local path.

    Looks up the run by `job_id` in <project_dir>/.magnolia/runs/*.yaml.
    rsyncs the remote_run_dir to local_run_dir, updates the yaml to
    lifecycle="fetched", returns a JSON summary.

    Call this when: you've checked a job (`check_job`), it returned
    `terminal: true`, and you want the output files locally before running
    `post_run_assess`."""
    from compchem_tools.tools import ssh_slurm
    if project_dir is None:
        return json.dumps(
            {"success": False, "error_kind": "run_record_missing",
             "error": "project_dir is required"},
            indent=2,
        )
    result = ssh_slurm.fetch(job_id=job_id, project_dir=project_dir)
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def poll_jobs(project_dir: str | None = None) -> str:
    """Run one async-lifecycle sweep: for each ssh-slurm job in lifecycle
    {submitted, running}, check sacct; on terminal state, fetch + assess
    (success) or fetch + capture (failure) or flag retry (infra failure).

    The same sweep runs automatically every MAGNOLIA_POLL_INTERVAL_MIN
    minutes in the background (default 5). Use this tool to force an
    immediate sweep — e.g. right after submitting a short job."""
    from compchem_tools.tools import poller
    pd = project_dir or os.environ.get("MAGNOLIA_PROJECT_DIR", ".")
    summary = poller.poll_jobs(pd)
    return json.dumps(summary, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def show_structure(
    path: str,
    residues: list[str] | None = None,
    project_dir: str | None = None,
) -> str:
    """Display a structure in the user's workbench viewer (no computation).

    `path` is project-relative or absolute (must live inside the active
    project). `residues` optionally names residues to show as ball-and-stick,
    e.g. ["PRO155", "LYS135", "A:MET156"] (NAME+number, optional "CHAIN:"
    prefix). Residues are validated against the file — a wrong number returns
    an error so you can correct it.

    Call this when: the user asks to see/show/view/inspect a structure, a
    pose, a design, or specific residues ("show me the best pose with
    PRO155/LYS135 in ball and stick")."""
    from compchem_tools.tools.view import show_structure as _show_structure
    result = _show_structure(path, residues=residues, project_dir=project_dir)
    return json.dumps(result, indent=2)


# ── v2 Session Management Tools ──────────────────────────────────────────────


@mcp.tool()
@captured(source="compchem-tools")
def run_progress(session_id: str) -> str:
    """Poll-based progress for long-running jobs. Returns module completion status,
    current module, percent complete, and latest scores.

    Call this when: checking intermediate progress of a long-running job via its session ID."""
    session = _active_sessions.get(session_id)
    if not session:
        return json.dumps({"error": f"Session not found: {session_id}"})

    run_dir = session["run_dir"]
    progress = parse_haddock_progress(run_dir)
    progress["session_id"] = session_id
    progress["session_status"] = session["status"]

    if progress["completed"] and session["status"] == "running":
        session["status"] = "completed"
        session["completed"] = datetime.now(timezone.utc).isoformat()
        progress["session_status"] = "completed"

    return json.dumps(progress, indent=2)


@mcp.tool()
@captured(source="compchem-tools")
def list_sessions() -> str:
    """List all active computation sessions with their status.

    Call this when: reviewing all currently tracked computation sessions to find a session ID or check status."""
    sessions = []
    for sid, info in _active_sessions.items():
        sessions.append(
            {
                "session_id": sid,
                "tool": info.get("tool", "unknown"),
                "run_dir": info.get("run_dir", ""),
                "status": info.get("status", "unknown"),
                "started": info.get("started", ""),
            }
        )
    return json.dumps(sessions, indent=2)


def _generate_session_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{ts}_{short}"


@mcp.tool()
def run_shell(
    cmd: str,
    cwd: str | None = None,
    project_dir: str | None = None,
    timeout: int | None = None,
    background: bool = False,
) -> dict:
    """Run a shell command via magnolia-run. magnolia-run writes the JSONL — this
    proxy intentionally does NOT use @captured to avoid double-logging.

    Foreground calls are capped at 110 s (default 90 s) so the server always
    answers before the MCP client aborts the call. For anything long-running use
    background=True (detached; returns pid + log_file immediately) or submit_job.

    Call this when: you need to invoke any shell command (gnina, gmx, ls, etc.).
    """
    return _run_shell(cmd, cwd=cwd, project_dir=project_dir, timeout=timeout, background=background)


# Start the async-lifecycle poller timer. Daemon thread; dies cleanly when
# the MCP subprocess exits. See poller.py for details.
from compchem_tools.tools import poller as _poller
_poller.run_poll_timer_background(os.environ.get("MAGNOLIA_PROJECT_DIR", "."))


def _is_client_disconnect(exc: BaseException) -> bool:
    """True iff every leaf of ``exc`` is a benign client-disconnect.

    When opencode aborts/cancels a tool call (or the connection drops) while a
    tool is still running, the tool's response is later written to a stream the
    peer has already closed. anyio raises ``ClosedResourceError`` /
    ``BrokenResourceError`` / ``EndOfStream`` deep inside the MCP request
    TaskGroup, surfacing as a (possibly nested) ``ExceptionGroup``. Those are
    benign — the peer is simply gone. A group containing ANY other exception
    returns ``False`` so genuine bugs still crash loudly. An empty leaf set
    returns ``False`` (never treat "nothing" as a disconnect)."""
    import anyio

    disconnect = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)

    def _leaves(e: BaseException):
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                yield from _leaves(sub)
        else:
            yield e

    leaves = list(_leaves(exc))
    return bool(leaves) and all(isinstance(leaf, disconnect) for leaf in leaves)


# A wedged/EOF stream makes ``mcp.run()`` fail in ~microseconds, repeatedly —
# the only thing the hot-loop guard must catch. A *legitimately cancelled*
# in-flight request, by contrast, did real work first (received, processed,
# then the peer vanished), so it takes meaningfully longer. The discriminator
# is therefore "did this iteration do essentially zero work", i.e. a tiny
# epsilon — NOT a full second. The original 1.0s window misread short-request
# cancels (a few hundred ms, fired seconds apart) as a wedged stream and exited
# the serve loop; opencode never respawns a dropped local MCP server, so all
# tools vanished for the session. Reproduced live 2026-06-20 (run 75a322b6).
_HOT_LOOP_FAST_SECONDS = 0.05
_HOT_LOOP_LIMIT = 3


def _serve_log_path() -> str:
    """Durable, server-wide diagnostic log for the serve loop.

    opencode pipes the server's stderr back over a socket but does NOT persist
    it anywhere greppable (verified 2026-06-20), so stderr-only logging is a
    black hole. We additionally append to a file under ``MAGNOLIA_ROOT`` so a
    server death actually leaves a trace."""
    import os

    root = os.environ.get("MAGNOLIA_ROOT") or os.getcwd()
    return os.path.join(root, "opencode_cc_mem", "logs", "compchem-tools-serve.log")


def _default_serve_log(msg: str) -> None:
    """Emit a serve-loop diagnostic to stderr AND a durable file. Never raises —
    logging must not be able to take down the loop it is trying to make safe."""
    import sys

    line = f"[compchem-tools] serve loop: {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        import os

        path = _serve_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _serve_resilient(run_fn=None, *, monotonic=None, log=None) -> None:
    """Run the MCP server, surviving a client disconnect mid-request.

    Unhandled, a disconnect-induced ``ClosedResourceError`` propagates out of
    ``mcp.run()`` and kills the whole process — taking the background job poller
    (a daemon thread) down with it. We swallow ONLY disconnect-shaped exception
    groups (see ``_is_client_disconnect``); anything else re-raises so real bugs
    are not masked.

    On a full connection close (stdin EOF) the next ``run_fn()`` returns
    immediately and the loop exits cleanly. If only a single request was
    cancelled, the connection is still live and serving resumes. A bound on
    consecutive *near-instant* (``< _HOT_LOOP_FAST_SECONDS``) failures prevents a
    hot loop if the stream is genuinely wedged, without misclassifying ordinary
    short-request cancellations as such. Every swallow/give-up is logged (stderr
    + durable file via ``_default_serve_log``) so this loop is observable instead
    of a silent black hole. ``log`` is injectable for tests."""
    import time

    run_fn = run_fn or mcp.run
    monotonic = monotonic or time.monotonic
    log = log or _default_serve_log
    consecutive_fast_failures = 0
    while True:
        started = monotonic()
        try:
            run_fn()
            return  # clean shutdown: client closed, nothing in flight
        except BaseException as exc:  # noqa: BLE001 — non-disconnects re-raised below
            if not _is_client_disconnect(exc):
                raise
            elapsed = monotonic() - started
            if elapsed < _HOT_LOOP_FAST_SECONDS:
                consecutive_fast_failures += 1
            else:
                consecutive_fast_failures = 0
            if consecutive_fast_failures >= _HOT_LOOP_LIMIT:
                log(
                    f"{consecutive_fast_failures} consecutive near-instant disconnects "
                    f"(<{_HOT_LOOP_FAST_SECONDS}s) — stream wedged, exiting cleanly."
                )
                return  # stream wedged/EOF — stop cleanly instead of hot-looping
            # connection may still be alive (single request cancelled) — resume
            log(f"swallowed client disconnect after {elapsed:.3f}s; resuming.")


if __name__ == "__main__":
    # Transport selection: stdio (opencode-spawned, legacy) or streamable-http
    # (supervised daemon). The HTTP daemon decouples server lifetime from the
    # opencode session — client aborts become dropped HTTP requests instead of
    # wedged-stdio exits. See softwares/bin/compchem-tools-daemon.sh.
    transport = os.environ.get("COMPCHEM_TOOLS_TRANSPORT", "stdio")
    if transport == "http":
        host = os.environ.get("COMPCHEM_TOOLS_HOST", "127.0.0.1")
        port = int(os.environ.get("COMPCHEM_TOOLS_PORT", "8001"))
        _serve_resilient(
            run_fn=lambda: mcp.run(transport="streamable-http", host=host, port=port)
        )
    else:
        _serve_resilient()
