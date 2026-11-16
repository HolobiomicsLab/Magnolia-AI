"""Load, validate, and project versioned executor capability evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


TABLE_PATH = Path(__file__).with_name("capabilities-p3.json")
REPOSITORY_ROOT = Path(__file__).parents[3]
CELL_STATUSES = frozenset({"observed", "synthetic-only", "unknown", "refused"})
CLIENTS = ("claude-code", "codex", "vibe")
ROLES = ("worker", "reviewer", "helper", "evaluator")
PREREQUISITE_CELLS = (
    "identity",
    "argv",
    "model_delivery",
    "discovery_dir",
    "trust",
    "hook_policy_digest",
    "native_schemas",
    "allowed_actions",
    "denied_actions",
    "resume",
    "caps",
    "usage_semantics",
)
ALL_CELLS = (*PREREQUISITE_CELLS, "quota_evidence")
OPTIONAL_CELLS = ("http_api_key_env",)


class CapabilityTableError(ValueError):
    """Raised when capability evidence is malformed or unsupported."""


def load_capability_table(
    path: Path = TABLE_PATH, *, repository_root: Path = REPOSITORY_ROOT
) -> dict[str, Any]:
    """Load and validate one capability table from JSON."""
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityTableError(f"cannot load capability table {path}: {exc}") from exc
    validate_capability_table(table, repository_root=repository_root)
    return table


def validate_capability_table(
    table: Mapping[str, Any], *, repository_root: Path = REPOSITORY_ROOT
) -> None:
    """Reject incomplete identities, unsupported statuses, and invalid evidence."""
    if table.get("schema_version") != 1:
        raise CapabilityTableError("schema_version must be 1")
    generated = table.get("generated_from")
    if not isinstance(generated, Mapping) or not generated.get("revision"):
        raise CapabilityTableError("generated_from.revision is required")
    rows = table.get("rows")
    if not isinstance(rows, list):
        raise CapabilityTableError("rows must be a list")
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        _validate_row(row, index, repository_root)
        key = (row["client"], row["role"])
        if key in seen:
            raise CapabilityTableError(f"duplicate capability row {key}")
        seen.add(key)
    expected = {(client, role) for client in CLIENTS for role in ROLES}
    if seen != expected:
        raise CapabilityTableError(f"capability rows differ from required matrix: {seen ^ expected}")


def project_role_capabilities(table: Mapping[str, Any]) -> dict[str, Any]:
    """Project table rows into the mapping consumed by launch capability checks."""
    projected: dict[str, Any] = {client: {} for client in CLIENTS}
    for row in table["rows"]:
        cells = row["cells"]
        blocking = [
            name for name in PREREQUISITE_CELLS if cells[name]["status"] != "observed"
        ]
        gate = row["gate"]
        gate_accepted = not blocking and gate["status"] == "observed"
        stale = _row_is_stale(row)
        model_delivery = _observed_value(cells["model_delivery"], {})
        allowed_actions = _observed_value(cells["allowed_actions"], [])
        if isinstance(allowed_actions, Mapping):
            allowed_actions = allowed_actions.get("capabilities", [])
        role_projection = {
            "accepted": gate_accepted and not stale,
            "gate_accepted": gate_accepted,
            "stale": stale,
            "trusted": _observed_value(cells["trust"], False) is True,
            "identity": _observed_value(cells["hook_policy_digest"]),
            "discovery_dir": _observed_value(cells["discovery_dir"]),
            "capabilities": allowed_actions,
            "model_isolation": model_delivery
            if isinstance(model_delivery, Mapping)
            else {"accepted": False},
            "blocking_cells": blocking,
            "reasons": [
                gate["reason"],
                *(["accepted live evidence is stale for launch"] if stale else []),
                *[f"{name}: {cells[name]['status']}" for name in blocking],
            ],
        }
        launch_identity = _project_launch_identity(row)
        if launch_identity is not None:
            role_projection["launch_identity"] = launch_identity
        projected[row["client"]][row["role"]] = role_projection
    return projected


def load_role_capabilities(
    path: Path = TABLE_PATH, *, repository_root: Path = REPOSITORY_ROOT
) -> dict[str, Any]:
    """Load the default table and return its launch-compatible projection."""
    return project_role_capabilities(
        load_capability_table(path, repository_root=repository_root)
    )


def _validate_row(row: Any, index: int, repository_root: Path) -> None:
    if not isinstance(row, Mapping):
        raise CapabilityTableError(f"row {index} must be an object")
    if row.get("client") not in CLIENTS or row.get("role") not in ROLES:
        raise CapabilityTableError(f"row {index} has an unknown client or role")
    if not isinstance(row.get("stale"), bool):
        raise CapabilityTableError(f"row {index}.stale must be a boolean")
    cells = row.get("cells")
    names = set(cells) if isinstance(cells, Mapping) else set()
    if not isinstance(cells, Mapping) or not set(ALL_CELLS) <= names:
        raise CapabilityTableError(f"row {index} must contain every capability cell")
    if names - set((*ALL_CELLS, *OPTIONAL_CELLS)):
        raise CapabilityTableError(f"row {index} contains an unknown capability cell")
    for name, cell in cells.items():
        _validate_cell(cell, f"row {index}.{name}", repository_root)
    identity = cells["identity"].get("value")
    if not isinstance(identity, Mapping):
        raise CapabilityTableError(f"row {index} identity must be an object")
    for field in ("executable", "version", "sha256"):
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise CapabilityTableError(f"row {index} identity requires {field}")
    gate = row.get("gate")
    _validate_cell(gate, f"row {index}.gate", repository_root)
    if not isinstance(gate.get("reason"), str) or not gate["reason"]:
        raise CapabilityTableError(f"row {index}.gate requires a reason")
    if gate["reason"].startswith(("D2:", "D4:")) and gate["status"] != "refused":
        raise CapabilityTableError(f"row {index}.gate D2/D4 refusal must be refused")


def _validate_cell(cell: Any, label: str, repository_root: Path) -> None:
    if not isinstance(cell, Mapping):
        raise CapabilityTableError(f"{label} must be an object")
    status = cell.get("status")
    if status not in CELL_STATUSES:
        raise CapabilityTableError(f"{label} has unknown status {status!r}")
    if status == "observed" and cell.get("value") in (None, "", [], {}):
        raise CapabilityTableError(f"{label} observed evidence requires a non-empty value")
    artifact = cell.get("artifact")
    if artifact is None:
        if status != "unknown":
            raise CapabilityTableError(f"{label} requires an artifact")
        return
    if not isinstance(artifact, str) or not artifact:
        raise CapabilityTableError(f"{label} artifact must be a repo-relative path")
    artifact_path = Path(artifact)
    if artifact_path.is_absolute() or ".." in artifact_path.parts:
        raise CapabilityTableError(f"{label} artifact must be repo-relative")
    resolved = repository_root / artifact_path
    if not resolved.is_file():
        raise CapabilityTableError(f"{label} artifact does not exist: {artifact}")
    if status == "observed" and _is_synthetic_artifact(resolved, artifact_path, label):
        raise CapabilityTableError(f"{label} observed evidence is synthetic: {artifact}")


def _is_synthetic_artifact(path: Path, relative_path: Path, label: str) -> bool:
    if "synthetic" in relative_path.parts:
        return True
    suffix = path.suffix.lower()
    if suffix not in {".json", ".jsonl"}:
        return False
    try:
        text = path.read_text(encoding="utf-8")
        payloads = [json.loads(text)] if suffix == ".json" else _parse_jsonl(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityTableError(f"{label} observed artifact failed to parse: {path}") from exc
    return any(_contains_synthetic_marker(payload) for payload in payloads)


def _parse_jsonl(text: str) -> list[Any]:
    payloads = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not payloads:
        raise json.JSONDecodeError("empty JSONL artifact", text, 0)
    return payloads


def _contains_synthetic_marker(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("synthetic") is True:
            return True
        return any(_contains_synthetic_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_synthetic_marker(item) for item in value)
    return False


def _observed_value(cell: Mapping[str, Any], default: Any = None) -> Any:
    return cell.get("value", default) if cell["status"] == "observed" else default


def _row_is_stale(row: Mapping[str, Any]) -> bool:
    """Treat an explicit stale mark or changed bound client identity as stale."""
    if row.get("stale") is not False:
        return True
    gate_value = row["gate"].get("value")
    if not isinstance(gate_value, Mapping):
        return False
    bound_identity = gate_value.get("client_identity")
    if bound_identity is None:
        return False
    return bound_identity != row["cells"]["identity"].get("value")


def _project_launch_identity(row: Mapping[str, Any]) -> dict[str, Any] | None:
    gate_value = row["gate"].get("value")
    if not isinstance(gate_value, Mapping):
        return None
    binding = gate_value.get("configuration_binding")
    if not isinstance(binding, Mapping) or not isinstance(
        binding.get("components"), Mapping
    ):
        return None
    identity = row["cells"]["identity"].get("value")
    if not isinstance(identity, Mapping):
        return None
    components = dict(binding["components"])
    components.update(
        client_version=identity.get("version"),
        client_sha256=identity.get("sha256"),
    )
    return {
        "schema_version": "magnolia-normalized-launch-identity-v1",
        "components": components,
    }
