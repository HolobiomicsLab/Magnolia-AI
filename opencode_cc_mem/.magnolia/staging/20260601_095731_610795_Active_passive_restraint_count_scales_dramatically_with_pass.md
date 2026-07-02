---
confidence: 0.85
created: '2026-06-01T09:57:31.610835+00:00'
description: "When generating ambig.tbl restraints for 5-mer peptides (KILDQ, KFERQ,\
  \ QRKEL, RKDLQ, RKELQ) binding to Hsc70 (chain A), the restraint count jumped from\
  \ ~13 (active-only) to 90\u201391 (active+passive) afte"
id: '20260601_095731_610795'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- restraint-count
- hsc70
- active-passive-selection
title: Active-passive restraint count scales dramatically with passive residue inclusion
tools:
- generate_restraints
type: parameter_guidance
updated: '2026-06-01T09:57:31.610843+00:00'
---

When generating ambig.tbl restraints for 5-mer peptides (KILDQ, KFERQ, QRKEL, RKDLQ, RKELQ) binding to Hsc70 (chain A), the restraint count jumped from ~13 (active-only) to 90–91 (active+passive) after fixing the actpass file. The exact counts: KILDQ=90, KFERQ=90, QRKEL=90, RKDLQ=91, RKELQ=90.

This reflects that passive residues are defined as all surface-accessible residues potentially contacting the peptide, dramatically expanding the allowed search space while still biasing toward the active site.

**CAVEAT:** Based on one run each for five 5-mer peptides against one Hsc70 receptor (chain A, PDB ~4800 atoms). Restraint counts will vary with receptor size, actpass definition, and the `--segid` assignment. The specific restraint counts (90–91) are for this Hsc70 surface definition only.
