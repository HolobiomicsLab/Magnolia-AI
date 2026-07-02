---
confidence: 0.9
created: '2026-06-01T09:57:32.380560+00:00'
description: Multiple validation calls show that input peptides must have chain ID
  'B' assigned. Six out of seven peptide PDBs validated successfully with chain_id=['B'],
  atom counts 42-53 atoms (all 5mers). One p
id: '20260601_095732_380538'
observation_count: 1
source: auto_extraction
tags:
- hsc70_peptides
- PDB-processing
- chain-assignment
- haddock3
- preprocess_pdb
title: PDB chain assignment requirement for haddock3 HADDOCK runs
tools:
- validate_structure
- preprocess_pdb
type: workflow_note
updated: '2026-06-01T09:57:32.380565+00:00'
---

Multiple validation calls show that input peptides must have chain ID 'B' assigned. Six out of seven peptide PDBs validated successfully with chain_id=['B'], atom counts 42-53 atoms (all 5mers). One peptide PDB (49 atoms, 3283 bytes) was initially missing chain IDs ('No chain IDs found in PDB') and was fixed by preprocessing with add_chain_id='B'. The receptor PDB (4792 atoms, chain A) validated cleanly. For haddock3 runs, chain A (receptor) and chain B (peptide) must be consistently assigned to generate valid ambig.tbl files via haddock3-restrain. The preprocessing step (preprocess_pdb with add_chain_id='B', remove_waters=True, fix_atom_names=True) reliably resolved chain ID issues, producing a 53-atom PDB (atom count increased from original 49, likely from adding missing atoms).
