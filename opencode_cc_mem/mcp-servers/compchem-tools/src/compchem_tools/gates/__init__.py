"""Stage gate validators for workflow transitions."""

from compchem_tools.gates.docking import (
    docking_inputs_ready,
    pose_valid,
    at_least_one_pocket,
)
from compchem_tools.gates.structure import (
    pdb_has_chain_id,
    file_size_nonzero,
    structure_parseable,
    qm_inputs_defined,
    scf_converged,
)
from compchem_tools.gates.covalent import (
    vinyl_isomers_exist,
    smarts_exactly_one_match,
    docked_poses_exist,
)

GATE_REGISTRY = {
    "docking_inputs_ready": docking_inputs_ready,
    "pose_valid": pose_valid,
    "at_least_one_pocket": at_least_one_pocket,
    "pdb_has_chain_id": pdb_has_chain_id,
    "file_size_nonzero": file_size_nonzero,
    "structure_parseable": structure_parseable,
    "vinyl_isomers_exist": vinyl_isomers_exist,
    "smarts_exactly_one_match": smarts_exactly_one_match,
    "docked_poses_exist": docked_poses_exist,
    "qm_inputs_defined": qm_inputs_defined,
    "scf_converged": scf_converged,
}
