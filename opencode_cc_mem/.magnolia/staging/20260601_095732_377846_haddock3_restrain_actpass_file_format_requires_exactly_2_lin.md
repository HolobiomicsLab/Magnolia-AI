---
confidence: 0.95
created: '2026-06-01T09:57:32.377896+00:00'
description: 'Symptoms: Failed to generate distance restraints for peptide KILDQ.
  The tool generate_restraints returned error: ''actpass file must have exactly 2
  lines (got 1)''. The actpass files at /home/tjiang/rep'
id: '20260601_095732_377846'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- generate_restraints
- haddock3-restrain
- actpass-format
- hsc70_peptides
- KILDQ
- KFERQ
- QRKEL
- RKDLQ
- RKELQ
- peptide-docking
title: haddock3-restrain actpass file format requires exactly 2 lines (not 1)
tools:
- generate_restraints
- validate_structure
type: error_resolution
updated: '2026-06-01T09:57:32.377905+00:00'
---

Symptoms: Failed to generate distance restraints for peptide KILDQ. The tool generate_restraints returned error: 'actpass file must have exactly 2 lines (got 1)'. The actpass files at /home/tjiang/repos/project_magnolia/opencode_cc_mem/projects/hsc70_new/runs/2026-06-01_KILDQ/receptor.actpass and ligand.actpass each contained only 1 line instead of the required 2 lines (active residues line + passive residues line). The error occurred for peptides KILDQ and KFERQ on first attempt, but RKDLQ and RKELQ worked successfully with 14 and 13 restraints respectively.

Cause: The actpass file format for haddock3-restrain requires two lines: the first line lists active residues, the second lists passive residues. The files contained only a single line, making them unparseable.

Fix: Reformat actpass files to have active residues on line 1 and passive residues on line 2. After reformatting, generate_restraints succeeded for KILDQ (90 restraints), KFERQ (90 restraints), QRKEL (90 restraints), RKDLQ (91 restraints), RKELQ (90 restraints).

Red herrings eliminated: The error was NOT caused by missing chain IDs in PDB files, incorrect segid settings, or problems with the PDB structure for KILDQ (all PDB files validated clean with chain B). It was purely an actpass file formatting issue. Earlier attempts (09:35:55, 09:36:06) failed; after fixing the format (09:52:46), the same molecular system succeeded.
