---
confidence: 0.95
created: '2026-06-01T09:57:31.608480+00:00'
description: '**Symptoms:** `generate_restraints` tool failed with error: "actpass
  file must have exactly 2 lines (got 1)". Attempted for run directory `2026-06-01_KILDQ`.


  **Cause:** The input `receptor.actpass` f'
id: '20260601_095731_608411'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- hsc70
- generate_restraints
- actpass-format
- restraint-generation
title: 'actpass file format requirement: exactly 2 lines'
tools:
- generate_restraints
- validate_structure
- haddock3
type: error_resolution
updated: '2026-06-01T09:57:31.608493+00:00'
---

**Symptoms:** `generate_restraints` tool failed with error: "actpass file must have exactly 2 lines (got 1)". Attempted for run directory `2026-06-01_KILDQ`.

**Cause:** The input `receptor.actpass` file for the Hsc70 receptor contained only 1 line. An actpass file must define both active (first line) and passive (second line) residue selections for HADDOCK3 restraint generation.

**Fix:** The actpass file was corrected to include a second line defining passive residues (e.g., all surface residues). After correction, `generate_restraints` succeeded, producing an `ambig.tbl` with 90 restraints (run `2026-06-01_KILDQ`) vs. the previous 13 when only active residues were used.

**Red herring eliminated:** The error was NOT due to incorrect PDB structure, wrong chain ID, missing segid, or path issues. All input structures (chain A Hsc70, chain B peptides) validated successfully.

**CAVEAT:** This applies to HADDOCK3 restraint generation via `haddock3-restrain` command (haddock3 - 2025.11.0). The actpass file requirement is specific to this tool; different restraint strategies (e.g., CNS-style tables) may have different formats.
