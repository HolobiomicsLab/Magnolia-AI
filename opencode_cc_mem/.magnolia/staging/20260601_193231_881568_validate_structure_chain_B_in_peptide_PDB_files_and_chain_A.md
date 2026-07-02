---
confidence: 0.7
created: '2026-06-01T19:32:31.881585+00:00'
description: "During validation of 5mer peptide structures (42\u201353 atoms each),\
  \ all were reported as valid PDB format. One file (RKDLQ) was flagged as invalid\
  \ due to 'No chain IDs found in PDB' (53 atoms, 3551 bytes"
id: '20260601_193231_881568'
observation_count: 1
source: auto_extraction
tags:
- validate_structure
- preprocess_pdb
- hsc70
- peptide-input
- process-management
title: validate_structure chain B in peptide PDB files and chain A missing chain IDs
  reported as valid but flagged
tools:
- validate_structure
- preprocess_pdb
type: workflow_note
updated: '2026-06-01T19:32:31.881588+00:00'
---

During validation of 5mer peptide structures (42–53 atoms each), all were reported as valid PDB format. One file (RKDLQ) was flagged as invalid due to 'No chain IDs found in PDB' (53 atoms, 3551 bytes). After preprocessing with preprocess_pdb (add_chain_id=B, remove_waters=True, fix_atom_names=True), it became valid with chain 'B'. The other files already had chain B assigned.

Validation of full hsc70 receptor: 4792 atoms, chain A, valid.

CAVEAT: This applies only to the 5 PDB files in /home/tjiang/.../hsc70_new/raw_input — file naming convention matters (RKDLQ vs RKDLQ_clean leads to different file selection).
