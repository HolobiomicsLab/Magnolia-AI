---
confidence: 0.9
created: '2026-06-01T09:57:31.612401+00:00'
description: 'For peptide RKDLQ, the raw input PDB (53 atoms) had no chain IDs and
  was flagged `valid: false` with issue "No chain IDs found in PDB". Running `preprocess_pdb`
  with `add_chain_id=B` and `remove_water'
id: '20260601_095731_612371'
observation_count: 1
source: auto_extraction
tags:
- pdb-processing
- hsc70
- peptide-preparation
- chain-id-assignment
title: 'Preprocessing order: remove waters and add chain IDs before validation'
tools:
- preprocess_pdb
- validate_structure
type: workflow_note
updated: '2026-06-01T09:57:31.612408+00:00'
---

For peptide RKDLQ, the raw input PDB (53 atoms) had no chain IDs and was flagged `valid: false` with issue "No chain IDs found in PDB". Running `preprocess_pdb` with `add_chain_id=B` and `remove_waters=True` produced `RKDLQ_clean.pdb` that validated successfully (chain B, 53 atoms). The chain ID assignment is essential because HADDOCK3 restraint generation requires distinct chain/segid for receptor (A) and peptide (B).

**CAVEAT:** This applies to single-chain peptide PDB files from this project. For multi-chain or already-valid PDB inputs, chain ID assignment may not be needed.
