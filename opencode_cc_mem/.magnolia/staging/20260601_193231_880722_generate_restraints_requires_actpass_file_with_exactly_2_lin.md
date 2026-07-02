---
confidence: 0.7
created: '2026-06-01T19:32:31.880739+00:00'
description: Initial attempts to generate ambiguous restraints for KILDQ failed with
  'actpass file must have exactly 2 lines (got 1)'. The actpass file for the receptor
  had only 1 line. Once the actpass file was c
id: '20260601_193231_880722'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- generate_restraints
- actpass
- hsc70
- restraint-generation
title: generate_restraints requires actpass file with exactly 2 lines (not 1)
tools:
- generate_restraints
type: parameter_guidance
updated: '2026-06-01T19:32:31.880743+00:00'
---

Initial attempts to generate ambiguous restraints for KILDQ failed with 'actpass file must have exactly 2 lines (got 1)'. The actpass file for the receptor had only 1 line. Once the actpass file was corrected, restraint generation succeeded producing ambig.tbl files with:
- For SBD-predicted peptides: 13–14 restraints (peptides KILDQ, KFERQ, QRKEL, RKDLQ, RKELQ)
- For full 5mer chains with bound positioning: 90–91 restraints (same set of peptides)

The 13–14 restraint count corresponds to sparse (surface-matching) positioning; 90–91 corresponds to bound-state specific contact restraints.

CAVEAT: The 13/90 restraint counts apply specifically to 5-mer peptides on hsc70 (4792 atoms, chain A). These numbers will differ for different peptide lengths or receptor structures. The 'exactly 2 lines' requirement is a general actpass format constraint for haddock3.
