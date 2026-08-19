"""Structure-display tool: ask the Ad Verum workbench to show a structure.

This is a UI-side-effect tool — it runs no computation. It validates the
request (file exists, parses; requested residues actually exist in the file)
and returns a JSON payload the web frontend turns into an artifact-panel
structure view. Residue validation matters: the agent often gets residue
numbers from memory or papers, and a silent typo would just silently not
render — instead the tool errors and the agent corrects itself.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

STRUCTURE_EXTS = {".pdb", ".ent", ".cif", ".mmcif"}

# "PRO155", "LYS135", "A:MET156" (chain-qualified), "155" (bare number).
_RES_RE = re.compile(r"^(?:(\w):)?([A-Z]{0,3})(\d+)$", re.IGNORECASE)


def _project_root() -> Path:
    """The active project's directory.

    MAGNOLIA_ROOT / MAGNOLIA_PROJECT_DIR are baked into opencode.json by the
    backend on every project switch, so the MCP server always sees the active
    project. The env has held two shapes across deployments:
    MAGNOLIA_PROJECT_DIR relative to MAGNOLIA_ROOT, or relative to
    opencode_cc_mem (the server's cwd) — accept either, preferring whichever
    dir actually exists."""
    root = os.environ.get("MAGNOLIA_ROOT", "")
    proj = os.environ.get("MAGNOLIA_PROJECT_DIR", "")
    if root and proj:
        candidates = [
            (Path(root) / proj),
            (Path(root) / "opencode_cc_mem" / proj),
        ]
        for c in candidates:
            if c.is_dir():
                return c.resolve()
        return candidates[0].resolve()
    if root:
        return Path(root).resolve()
    return Path.cwd()


def _resolve_path(path: str, project_dir: str | None) -> tuple[Path, Path, str] | None:
    """→ (absolute path, project root, project-relative posix path) or None."""
    root = Path(project_dir).resolve() if project_dir else _project_root()
    p = Path(path)
    abs_p = p if p.is_absolute() else (root / p)
    abs_p = abs_p.resolve()
    try:
        rel = abs_p.relative_to(root)
    except ValueError:
        return None
    return abs_p, root, rel.as_posix()


def _parse_residue(spec: str) -> tuple[str | None, int] | None:
    """'A:MET156' → ('A', 156); 'PRO155' → (None, 155)."""
    m = _RES_RE.match(spec.strip())
    if not m:
        return None
    chain = m.group(1) or None
    return chain, int(m.group(3))


def _file_residues(p: Path) -> dict[int, set[str]]:
    """Scan ATOM/HETATM records → {resi: {chain}} (PDB; CIF not scanned —
    residue checks are skipped there rather than paying a full mmCIF parse)."""
    residues: dict[int, set[str]] = {}
    if p.suffix.lower() not in (".pdb", ".ent"):
        return residues
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return residues
    for line in text.splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 26:
            try:
                resi = int(line[22:26])
            except ValueError:
                continue
            chain = line[21] if line[21].strip() else ""
            residues.setdefault(resi, set()).add(chain)
    return residues


def _scan_resnames(p: Path, want: dict[int, list[str | None]]) -> dict[int, str]:
    """resi → resname for the requested residues (PDB only; best-effort)."""
    out: dict[int, str] = {}
    if p.suffix.lower() not in (".pdb", ".ent"):
        return out
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 26:
            try:
                resi = int(line[22:26])
            except ValueError:
                continue
            if resi in want and resi not in out:
                out[resi] = line[17:20].strip()
    return out


def show_structure(
    path: str,
    residues: list[str] | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Validate a structure-view request; the payload drives the web workbench."""
    resolved = _resolve_path(path, project_dir)
    if resolved is None:
        return {
            "view": False,
            "error": f"path is outside the active project: {path}",
        }
    abs_p, _root, rel = resolved
    if not abs_p.is_file():
        return {"view": False, "error": f"file not found: {path}"}
    if abs_p.suffix.lower() not in STRUCTURE_EXTS:
        return {
            "view": False,
            "error": f"not a structure file ({abs_p.suffix or 'no ext'}): {path}",
        }

    picks: list[dict[str, Any]] = []
    if residues:
        file_res = _file_residues(abs_p)
        want: dict[int, list[str | None]] = {}
        for spec in residues:
            parsed = _parse_residue(spec)
            if parsed is None:
                return {"view": False, "error": f"unrecognized residue spec: {spec!r} (use e.g. PRO155 or A:MET156)"}
            chain, resi = parsed
            want.setdefault(resi, []).append(chain)
        # Validate everything before returning anything (all-or-nothing).
        for resi, chains in want.items():
            if file_res and resi not in file_res:
                return {"view": False, "error": f"residue {resi} not present in {abs_p.name}"}
            for chain in chains:
                if file_res and chain and chain not in file_res.get(resi, set()):
                    return {
                        "view": False,
                        "error": f"residue {resi} has no chain {chain} in {abs_p.name} (chains: {sorted(file_res.get(resi, set())) or '?'})",
                    }
        resnames = _scan_resnames(abs_p, want)
        seen: set[tuple[str | None, int]] = set()
        for spec in residues:
            chain, resi = _parse_residue(spec)  # re-parse; validated above
            if (chain, resi) in seen:
                continue
            seen.add((chain, resi))
            picks.append(
                {
                    "chain": chain or None,
                    "resi": resi,
                    "resn": resnames.get(resi, ""),
                }
            )

    return {
        "view": True,
        "path": rel,
        "name": abs_p.name,
        "residues": picks,
    }
