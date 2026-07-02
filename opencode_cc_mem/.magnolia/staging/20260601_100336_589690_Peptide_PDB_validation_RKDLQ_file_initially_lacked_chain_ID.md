---
confidence: 0.8
created: '2026-06-01T10:03:36.589737+00:00'
description: 'During preprocessing of 5mer PDB files for motif sequences KILDQ, KFERQ,
  QRKEL, RKDLQ, and RKELQ:

  - One peptide file (RKDLQ) was initially invalid because it had no chain IDs (atom_count=53,
  file_size'
id: '20260601_100336_589690'
observation_count: 1
source: auto_extraction
tags:
- peptide-preprocessing
- PDB-chain-ID
- validate_structure
- preprocess_pdb
- 5mers
- opencode_cc_mem
- Hsc70
title: 'Peptide PDB validation: RKDLQ file initially lacked chain ID B'
tools:
- validate_structure
- preprocess_pdb
type: workflow_note
updated: '2026-06-01T10:03:36.589749+00:00'
---

During preprocessing of 5mer PDB files for motif sequences KILDQ, KFERQ, QRKEL, RKDLQ, and RKELQ:
- One peptide file (RKDLQ) was initially invalid because it had no chain IDs (atom_count=53, file_size=3551 bytes). The validate_structure tool reported "No chain IDs found in PDB".
- The preprocess_pdb tool with add_chain_id=B fixed this (output RKDLQ_clean.pdb, maintaining 53 atoms, file_size=3554 bytes, now valid with chain B).
- The other four peptides (KILDQ, KFERQ, QRKEL, RKELQ) already had chain B set (42-49 atoms, 3318-3634 bytes) and required no preprocessing.
- CAVEAT: This is specific to this particular peptide dataset. New peptide PDBs from other sources may also lack chain IDs and need preprocess_pdb with add_chain_id.
