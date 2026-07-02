---
confidence: 0.9
created: '2026-06-01T19:32:33.504439+00:00'
description: Using haddock3-restrain with actpass files generated from Hsc70 SBD (chain
  A) and 5-mer peptides (chain B) produces 90-91 unambiguous restraints (ambig.tbl)
  when passive residues are included. This is
id: '20260601_193233_504419'
observation_count: 1
source: auto_extraction
tags:
- haddock3-restrain
- haddock3
- restraint-generation
- ambig-tbl
- Hsc70
- SBD
- peptide-docking
- active-passive
title: 'HADDOCK3 restraint generation: passive-to-active ratio from actpass files
  yields 90+ restraints for SBD-peptide docking'
tools:
- generate_restraints
- haddock3
type: workflow_note
updated: '2026-06-01T19:32:33.504444+00:00'
---

Using haddock3-restrain with actpass files generated from Hsc70 SBD (chain A) and 5-mer peptides (chain B) produces 90-91 unambiguous restraints (ambig.tbl) when passive residues are included. This is calculated from 13 active residues in receptor SBD × 7 possible interacting residues on the peptide (chain B: 1 active + many passive). Smaller restraint count (13-14) occurs when only active residues are defined (single-line actpass).

CAVEAT: The large restraint count (~90) assumes both active and passive residues are defined in actpass files. Validation on KILDQ, KFERQ, QRKEL, RKELQ, RKDLQ all gave 90-91 restraints. This is specific to Hsc70 SBD (single domain) + 5-mer peptide docking scenario.
