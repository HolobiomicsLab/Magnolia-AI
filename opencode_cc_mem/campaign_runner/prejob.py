"""Mechanical input checks before job submission."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class PrejobError(Exception):
    """Raised when job inputs fail verification."""


_SECTION_RE = re.compile(r"^\[([A-Za-z0-9_]+)\]\s*$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$")
_RESI_RE = re.compile(r"\bresi\s+(\d+)\b")


def validate_prejob(working_dir: str | Path, tool: str | None = None) -> list[str]:
    directory = Path(working_dir).resolve()
    errors: list[str] = []
    if not directory.is_dir():
        raise PrejobError(f"working directory does not exist: {directory}")
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if "output" in relative.parts:
            continue
        if path.is_symlink():
            errors.append(f"symlink input is not allowed: {relative}")
            continue
        if path.is_file() and path.stat().st_size == 0:
            errors.append(f"empty input: {relative}")
    for path in directory.rglob("*.pdb"):
        if "output" in path.relative_to(directory).parts:
            continue
        errors.extend(_validate_pdb(path))
    for path in directory.rglob("*.sdf"):
        if "output" in path.relative_to(directory).parts:
            continue
        errors.extend(_validate_sdf(path))
    normalized_tool = (tool or "").strip().lower()
    has_haddock_config = (directory / "config.cfg").is_file()
    if has_haddock_config:
        if not normalized_tool:
            errors.append("tool is required when config.cfg is present")
        elif normalized_tool != "haddock3":
            errors.append(
                f"config.cfg requires tool haddock3, got {normalized_tool}"
            )
        errors.extend(_validate_haddock3(directory))
    elif normalized_tool == "haddock3":
        errors.extend(_validate_haddock3(directory))
    if errors:
        raise PrejobError("; ".join(errors))
    return []


def _validate_pdb(path: Path) -> list[str]:
    errors: list[str] = []
    atoms = 0
    residues: dict[tuple[str, str], set[int]] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"cannot read {path.name}: {exc}"]
    for line in lines:
        if not line.startswith(("ATOM", "HETATM")):
            continue
        atoms += 1
        if len(line) < 22 or not line[21].strip():
            errors.append(f"{path.name}: missing chain ID")
            break
        chain = line[21]
        resseq = line[22:26].strip()
        try:
            resseq_int = int(resseq)
        except ValueError:
            errors.append(f"{path.name}: invalid residue number '{resseq}'")
            continue
        residues.setdefault((chain, path.name), set()).add(resseq_int)
    if atoms == 0:
        errors.append(f"{path.name}: no atoms")
    for (chain, name), values in residues.items():
        if not values:
            errors.append(f"{name}: chain {chain} has no residues")
    return errors


def _validate_sdf(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"cannot read {path.name}: {exc}"]
    blocks = [block for block in text.split("$$$$") if block.strip()]
    if not blocks:
        return [f"{path.name}: no SDF records"]
    errors: list[str] = []
    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 4:
            errors.append(f"{path.name} record {index}: truncated header")
            continue
        try:
            atoms = int(lines[3][0:3])
            bonds = int(lines[3][3:6])
        except (ValueError, IndexError):
            errors.append(f"{path.name} record {index}: invalid counts line")
            continue
        atom_lines = [line for line in lines[4 : 4 + atoms] if line.strip()]
        if len(atom_lines) != atoms:
            errors.append(f"{path.name} record {index}: atom table is truncated")
        if bonds < 0:
            errors.append(f"{path.name} record {index}: negative bond count")
    return errors


def _parse_config(path: Path) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    errors: list[str] = []
    section = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {}, [f"cannot read {path.name}: {exc}"]
    for number, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1)
            continue
        key_match = _KEY_RE.match(line)
        if not key_match:
            if not line.startswith(("(", ")", "{", "}")):
                errors.append(f"config.cfg line {number} is not key = value")
            continue
        key, value = key_match.groups()
        values[f"{section}.{key}" if section else key] = value.strip()
    return values, errors


def _molecules(values: dict[str, Any]) -> list[str]:
    raw = values.get("molecules")
    if not isinstance(raw, str):
        return []
    return [part.strip('"\' ') for part in raw.strip("[]").split(",") if part.strip()]


def _validate_haddock3(directory: Path) -> list[str]:
    config_path = directory / "config.cfg"
    if not config_path.is_file():
        return ["config.cfg is missing"]
    values, errors = _parse_config(config_path)
    run_dir = values.get("run_dir", "")
    if run_dir:
        run_dir = str(run_dir).strip("'\"")
        run_path = Path(run_dir)
        if run_path.is_absolute() or ".." in run_path.parts:
            errors.append("run_dir must be a relative path without '..'")
    molecules = _molecules(values)
    if not molecules:
        errors.append("config.cfg has no molecules list")
    for name in molecules:
        path = directory / name
        if not path.is_file():
            errors.append(f"molecule missing: {name}")
        elif path.stat().st_size == 0:
            errors.append(f"molecule is empty: {name}")
    actpass_by_file: dict[Path, set[int]] = {}
    actpass_values: set[int] = set()
    actpass_files = sorted(directory.glob("*.actpass"))
    for path in actpass_files:
        lines = [
            line
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        if len(lines) != 2:
            errors.append(f"{path.name}: must contain exactly two residue lines")
            continue
        residues_for_file: set[int] = set()
        for number, line in enumerate(lines, 1):
            try:
                residues = {int(item) for item in line.split()}
            except ValueError:
                errors.append(f"{path.name} line {number}: non-integer residue")
                continue
            if not residues:
                errors.append(f"{path.name} line {number}: no residues")
            residues_for_file.update(residues)
        actpass_by_file[path] = residues_for_file
        actpass_values.update(residues_for_file)
    ambig_path = directory / "ambig.tbl"
    if ambig_path.is_file() and actpass_values:
        ambig_values = {
            int(match.group(1))
            for match in _RESI_RE.finditer(
                ambig_path.read_text(encoding="utf-8", errors="replace")
            )
        }
        unknown = sorted(ambig_values - actpass_values)
        if unknown:
            errors.append(
                "ambig.tbl references residues outside actpass: "
                + ", ".join(str(value) for value in unknown[:10])
            )
    weights_path = directory / "weights_params.json"
    if weights_path.is_file():
        errors.extend(_validate_weights(weights_path))
    start = values.get("flexref.fle_sta_2") or values.get("mdref.fle_sta_2")
    end = values.get("flexref.fle_end_2") or values.get("mdref.fle_end_2")
    segment = values.get("flexref.fle_seg_2") or values.get("mdref.fle_seg_2")
    if start or end or segment:
        peptide_path = _select_peptide_pdb(directory, molecules, segment)
        if peptide_path is None:
            errors.append("cannot identify peptide PDB for fle_* checks")
        else:
            chain_residues = _pdb_chain_residues(peptide_path)
            segment_name = str(segment).strip("'\"") if segment else ""
            if segment_name:
                segments = _pdb_segments(peptide_path)
                if segments:
                    if segment_name not in segments:
                        errors.append(
                            f"peptide PDB has no segment {segment_name} "
                            f"(segments: {', '.join(sorted(segments))})"
                        )
                elif segment_name not in chain_residues:
                    errors.append(f"peptide PDB has no chain {segment_name}")
            try:
                start_value = int(str(start).strip("'\"")) if start else None
                end_value = int(str(end).strip("'\"")) if end else None
            except ValueError:
                errors.append("fle_sta_2/fle_end_2 must be integers")
                start_value = end_value = None
            chain = segment_name
            if chain not in chain_residues:
                chain = min(chain_residues, key=lambda key: len(chain_residues[key]), default="")
            peptide_residues = chain_residues.get(chain, set())
            if start_value is not None:
                first = min(peptide_residues) if peptide_residues else None
                if first != start_value:
                    errors.append(
                        f"fle_sta_2 {start_value} does not match the first "
                        f"peptide residue {first}"
                    )
            if end_value is not None:
                last = max(peptide_residues) if peptide_residues else None
                if last != end_value:
                    errors.append(
                        f"fle_end_2 {end_value} does not match the last "
                        f"peptide residue {last}"
                    )
            peptide_actpass = peptide_path.with_suffix(".actpass")
            if peptide_actpass in actpass_by_file and start_value is not None and end_value is not None:
                outside = sorted(
                    value
                    for value in actpass_by_file[peptide_actpass]
                    if value < start_value or value > end_value
                )
                if outside:
                    errors.append(
                        f"{peptide_actpass.name} has residues outside the "
                        f"fle range: {', '.join(str(value) for value in outside[:10])}"
                    )
    if (directory / "output").exists():
        errors.append("output directory already exists")
    return errors


def _validate_weights(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path.name}: invalid JSON: {exc}"]
    errors: list[str] = []

    def walk(item: Any, where: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                walk(child, f"{where}.{key}")
            return
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            errors.append(f"{path.name}: {where} must be numeric")

    walk(value, "weights")
    return errors


def _select_peptide_pdb(
    directory: Path,
    molecules: list[str],
    segment: Any,
) -> Path | None:
    chain = str(segment).strip("'\"") if segment else ""
    candidates: list[tuple[int, int, Path]] = []
    for name in molecules:
        if not name.lower().endswith(".pdb"):
            continue
        path = directory / name
        if not path.is_file():
            continue
        residues = _pdb_chain_residues(path)
        count = sum(len(values) for values in residues.values())
        chain_match = 1 if chain and chain in residues else 0
        candidates.append((chain_match, -count, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _pdb_segments(path: Path) -> set[str]:
    segments: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 76:
            continue
        segment = line[72:76].strip()
        if segment:
            segments.add(segment)
    return segments


def _pdb_chain_residues(path: Path) -> dict[str, set[int]]:
    residues: dict[str, set[int]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 26:
            continue
        chain = line[21].strip() or " "
        try:
            number = int(line[22:26].strip())
        except ValueError:
            continue
        residues.setdefault(chain, set()).add(number)
    return residues
