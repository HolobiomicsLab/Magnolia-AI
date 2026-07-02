"""Covalent docking chemistry tools: SMARTS validation, alkyne-to-vinyl conversion."""

import re
from pathlib import Path
from typing import Any


def smarts_validate(smarts: str, smiles: str | None = None) -> dict[str, Any]:
    """Validate a SMARTS pattern. Optionally check it matches a SMILES molecule.
    Returns validity status and match count if SMILES provided."""
    result: dict[str, Any] = {"smarts": smarts, "valid": False}

    # Basic syntactic validation
    bracket_count = 0
    paren_count = 0
    for ch in smarts:
        if ch == "[":
            bracket_count += 1
        elif ch == "]":
            bracket_count -= 1
        elif ch == "(":
            paren_count += 1
        elif ch == ")":
            paren_count -= 1

    if bracket_count != 0:
        result["error"] = "Unmatched brackets in SMARTS"
        return result
    if paren_count != 0:
        result["error"] = "Unmatched parentheses in SMARTS"
        return result

    # Check for ring closure digits consistency (exclude atom map numbers like :1)
    # Remove bracket contents first for ring closure check
    smarts_no_brackets = re.sub(r'\[.*?\]', '', smarts)
    ring_digits = re.findall(r'(\d)', smarts_no_brackets)
    digit_counts: dict[str, int] = {}
    for d in ring_digits:
        digit_counts[d] = digit_counts.get(d, 0) + 1
    for d, count in digit_counts.items():
        if count % 2 != 0:
            result["error"] = f"Ring closure digit '{d}' appears odd number of times"
            return result

    result["valid"] = True

    # If SMILES provided, try to count matches using RDKit (optional)
    if smiles:
        try:
            from rdkit import Chem
            mol = Chem.MolFromSmiles(smiles)
            pattern = Chem.MolFromSmarts(smarts)
            if mol is None:
                result["smiles_valid"] = False
                result["match_count"] = 0
            elif pattern is None:
                result["smarts_parseable"] = False
                result["match_count"] = 0
            else:
                matches = mol.GetSubstructMatches(pattern)
                result["smiles_valid"] = True
                result["smarts_parseable"] = True
                result["match_count"] = len(matches)
                result["matches"] = [list(m) for m in matches]
        except ImportError:
            result["rdkit_available"] = False
            result["match_count"] = None

    return result


def alkyne_to_vinyl(
    alkyne_smiles: str,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Convert an alkyne-containing SMILES to Z and E vinyl isomer SMILES.
    Used for covalent docking of alkyne warheads.
    Returns Z and E isomer SMILES strings."""
    result: dict[str, Any] = {
        "input_smiles": alkyne_smiles,
        "success": False,
    }

    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem

        mol = Chem.MolFromSmiles(alkyne_smiles)
        if mol is None:
            result["error"] = "Could not parse SMILES"
            return result

        # Find triple bonds
        triple_bonds = []
        for bond in mol.GetBonds():
            if bond.GetBondTypeAsDouble() == 3.0:
                triple_bonds.append((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))

        if not triple_bonds:
            result["error"] = "No triple bond found in input SMILES"
            return result

        # Take first triple bond and convert to Z/E double bond
        a1_idx, a2_idx = triple_bonds[0]
        rw = Chem.RWMol(mol)
        bond = rw.GetBondBetweenAtoms(a1_idx, a2_idx)
        rw.RemoveBond(a1_idx, a2_idx)

        isomers = []
        for stereo_tag in ["Z", "E"]:
            rw2 = Chem.RWMol(rw)
            from rdkit.Chem import BondStereo
            if stereo_tag == "Z":
                rw2.AddBond(a1_idx, a2_idx, Chem.BondType.DOUBLE)
                b = rw2.GetBondBetweenAtoms(a1_idx, a2_idx)
                b.SetStereo(BondStereo.STEREOZ)
            else:
                rw2.AddBond(a1_idx, a2_idx, Chem.BondType.DOUBLE)
                b = rw2.GetBondBetweenAtoms(a1_idx, a2_idx)
                b.SetStereo(BondStereo.STEREOE)

            try:
                Chem.SanitizeMol(rw2)
                smi = Chem.MolToSmiles(rw2)
                isomers.append({"stereo": stereo_tag, "smiles": smi})
            except Exception:
                isomers.append({"stereo": stereo_tag, "smiles": None, "error": "sanitization failed"})

        result["isomers"] = isomers
        result["triple_bonds_found"] = len(triple_bonds)
        result["success"] = any(i["smiles"] is not None for i in isomers)

        # Optionally write to files
        if output_dir:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            for iso in isomers:
                if iso["smiles"]:
                    (out / f"vinyl_{iso['stereo']}.smi").write_text(iso["smiles"])
            result["output_dir"] = str(out)

        return result

    except ImportError:
        # Fallback without RDKit: simple string substitution heuristic
        return _alkyne_to_vinyl_heuristic(alkyne_smiles)


def _alkyne_to_vinyl_heuristic(smiles: str) -> dict[str, Any]:
    """Heuristic fallback when RDKit is not available."""
    result: dict[str, Any] = {
        "input_smiles": smiles,
        "success": False,
        "note": "RDKit not available, using heuristic substitution",
    }

    # Replace C#C with C/C=C (E) and C\C=C (Z) — very simplified
    if "#" not in smiles:
        result["error"] = "No triple bond (#) found in SMILES"
        return result

    e_smiles = smiles.replace("#", "/", 1).replace("#", "\\", 1) if smiles.count("#") == 1 else None
    z_smiles = smiles.replace("#", "\\", 1).replace("#", "/", 1) if smiles.count("#") == 1 else None

    # Simple single-triple-bond case
    parts = smiles.split("#")
    if len(parts) == 2:
        result["isomers"] = [
            {"stereo": "Z", "smiles": f"{parts[0]}/{parts[1]}"},
            {"stereo": "E", "smiles": f"{parts[0]}/{parts[1]}"},
        ]
        result["success"] = True

    return result
