---
confidence: 0.8
created: '2026-06-05T08:28:51.898370+00:00'
description: 'Standard protocol: peptides were constructed using tleap with ff14SB
  force field in Amber format, then converted to HADDOCK3-compatible PDB format. Key
  steps: 1) Build peptide in extended conformation'
id: '20260605_082851_898337'
observation_count: 2
observed_in_sessions:
- 2026-06-16_062532
source: auto_extraction
tags:
- haddock3
- hsc70
- tleap
- ff14SB
- peptide-construction
title: Peptide construction method and force field for HADDOCK3 docking on Hsc70
tools:
- haddock3
- tleap
type: parameter_guidance
updated: '2026-06-16T09:49:00.055949+00:00'
---

Standard protocol: peptides were constructed using tleap with ff14SB force field in Amber format, then converted to HADDOCK3-compatible PDB format. Key steps: 1) Build peptide in extended conformation via tleap, 2) Add hydrogen atoms and assign protonation states at pH 7.4, 3) Convert to PDB for HADDOCK3 input. This method was recorded as working for KILDQ and 3 additional peptides.

CAVEAT: Validated only for Hsc70-binding peptides (6-10 residues) with standard amino acids. Not suitable for peptides with non-standard residues, post-translational modifications, or cyclic peptides. Force field choice (ff14SB) may not be optimal for other protein targets.

## Observation 2 (2026-06-16)

## How to build a peptide for HADDOCK3 docking

Use AMBER's **tleap** from `ambertools` (conda env: `ambertools`), not hand-written coordinates.

### Workflow (from leap.log, 2026-06-11)

```bash
conda activate ambertools
tleap
```

In tleap:
```
source leaprc.protein.ff14SB
peptide = sequence { HIS ALA MET ASN GLY }
savepdb peptide /path/to/output.pdb
quit
```

### Why
- tleap generates proper bond lengths, angles, and atom naming using ff14SB force field
- Hand-built PDBs with approximate coordinates will fail HADDOCK3's topoaa parameterization
- This was used for all 6-mer extension peptides (KFERQ_N-LYS, KFERQ_N-GLY, etc.) in June 2026

### CAVEAT
This is for linear peptides only. For cyclic or modified peptides, different parameterization (ACPYPE or custom topology) may be needed.
