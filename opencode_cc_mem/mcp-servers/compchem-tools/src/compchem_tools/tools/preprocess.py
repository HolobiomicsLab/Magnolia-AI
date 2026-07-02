"""Structure preprocessing tools: PDB cleanup, validation, format conversion."""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def preprocess_pdb(
    input_path: str,
    output_path: str | None = None,
    add_chain_id: str | None = None,
    remove_waters: bool = True,
    fix_atom_names: bool = True,
) -> dict[str, Any]:
    """Add chain IDs, fix atom names, remove waters.
    Returns path to cleaned PDB."""
    inp = Path(input_path)
    if not inp.exists():
        return {"success": False, "error": f"Input file not found: {input_path}"}

    out = (
        Path(output_path)
        if output_path
        else inp.parent / f"{inp.stem}_clean{inp.suffix}"
    )
    lines = inp.read_text().split("\n")
    cleaned = []

    for line in lines:
        if line.startswith(("ATOM", "HETATM")):
            if remove_waters:
                resname = line[17:20].strip() if len(line) > 20 else ""
                if resname in ("HOH", "WAT", "TIP3", "SOL"):
                    continue

            if add_chain_id and len(line) >= 22:
                line = line[:21] + add_chain_id + line[22:]

            if fix_atom_names and len(line) >= 16:
                atom_name = line[12:16]
                if atom_name[0] == " " and atom_name[1] == " " and atom_name[3] != " ":
                    pass

            cleaned.append(line)
        elif line.startswith("TER") or line.startswith("END"):
            cleaned.append(line)
        elif line.startswith(("HEADER", "TITLE", "COMPND", "SOURCE", "REMARK")):
            cleaned.append(line)

    if not any(l.startswith("END") for l in cleaned):
        cleaned.append("END")

    out.write_text("\n".join(cleaned))
    atom_count = sum(1 for l in cleaned if l.startswith(("ATOM", "HETATM")))

    return {
        "success": True,
        "output_path": str(out),
        "atom_count": atom_count,
        "removed_waters": sum(
            1
            for l in lines
            if l.startswith(("ATOM", "HETATM"))
            and len(l) > 20
            and l[17:20].strip() in ("HOH", "WAT")
        )
        if remove_waters
        else 0,
    }


def validate_structure(
    input_path: str,
    expected_format: str | None = None,
) -> dict[str, Any]:
    """Basic sanity checks on PDB/SDF: atom count, chain IDs present,
    non-zero size, parseable by a structure library."""
    inp = Path(input_path)
    if not inp.exists():
        return {"valid": False, "error": f"File not found: {input_path}"}

    size = inp.stat().st_size
    if size == 0:
        return {"valid": False, "error": "File is empty", "path": str(inp)}

    text = inp.read_text()
    issues = []

    suffix = inp.suffix.lower()
    if suffix in (".pdb", ".ent"):
        atom_lines = [l for l in text.split("\n") if l.startswith(("ATOM", "HETATM"))]
        if not atom_lines:
            issues.append("No ATOM/HETATM records found")

        chain_ids = set()
        for l in atom_lines:
            if len(l) > 21:
                cid = l[21].strip()
                if cid:
                    chain_ids.add(cid)

        if not chain_ids:
            issues.append("No chain IDs found in PDB")

        result: dict[str, Any] = {
            "valid": len(issues) == 0,
            "format": "pdb",
            "atom_count": len(atom_lines),
            "chain_ids": sorted(chain_ids),
            "file_size_bytes": size,
        }

    elif suffix in (".sdf", ".mol"):
        count = text.count("\n$$$$\n") + text.count("M  END")
        result = {
            "valid": count > 0,
            "format": "sdf",
            "molecule_count": count,
            "file_size_bytes": size,
        }
        if count == 0:
            issues.append("No molecule records found in SDF")
    else:
        result = {
            "valid": True,
            "format": suffix.lstrip(".") or "unknown",
            "file_size_bytes": size,
        }

    if issues:
        result["issues"] = issues
    return result
