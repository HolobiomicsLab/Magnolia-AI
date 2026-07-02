---
confidence: 0.85
created: '2026-06-01T10:03:36.583314+00:00'
description: 'Using haddock3-restrain with manually corrected actpass files (2 lines
  each for receptor and peptide):

  - With ''ground_state'' contact list (fewer passive residues): 13-14 ambiguous restraints
  (exact va'
id: '20260601_100336_583276'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- haddock3-restrain
- parameter-guidance
- Hsc70
- 5mer-peptide
- opencode_cc_mem
- ambiguous-restraints
title: 'Restraint count for Hsc70 anchor-peptide docking with actpass files: 13-91
  restraints depending on actpass scope'
tools:
- generate_restraints
type: parameter_guidance
updated: '2026-06-01T10:03:36.583325+00:00'
---

Using haddock3-restrain with manually corrected actpass files (2 lines each for receptor and peptide):
- With 'ground_state' contact list (fewer passive residues): 13-14 ambiguous restraints (exact values: KILDQ=13, KFERQ=13, QRKEL=13, RKDLQ=14, RKELQ=13).
- With an expanded (full-surface likely) contact list: 90-91 ambiguous restraints (KILDQ=90, KFERQ=90, QRKEL=90, RKDLQ=91, RKELQ=90).
- Seg IDs used: segid_one=A (receptor/Hsc70 chain), segid_two=B (peptide chain).
- CAVEAT: These numbers are specific to the Hsc70 nucleotide-binding domain (PDB file with 4792 atoms, single chain A) and 5-residue peptides (40-53 atoms, chain B). The contact list definition (which residues are 'passive' in the actpass file) dramatically changes restraint count. Not generalizable to other systems without re-deriving actpass files.
