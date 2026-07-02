---
confidence: 0.8
created: '2026-06-17T19:26:20.626841+00:00'
description: Per-residue gas-phase energy (MM-GBSA decomposition) does not correlate
  with binding affinity for the CDK4-derived 9-mer Ac-TGGFQMALT (one of the tightest
  known Hsc70 binders, Torielli et al. 2025). T
id: '20260617_192620_626828'
observation_count: 1
source: auto_extraction
tags:
- MM-GBSA
- per-residue
- hydrophobic-effect
- TGGFQMALT
- Hsc70
- peptide-scoring
- 4po2
title: "MM-GBSA per-residue gas-phase energy cannot rank Hsc70 binding \u2014 TGGFQMALT\
  \ case study"
tools:
- Amber:MMPBSA.py
- haddock3
type: parameter_guidance
updated: '2026-06-17T19:26:20.626841+00:00'
---

Per-residue gas-phase energy (MM-GBSA decomposition) does not correlate with binding affinity for the CDK4-derived 9-mer Ac-TGGFQMALT (one of the tightest known Hsc70 binders, Torielli et al. 2025). The hydrophobic effect, which is not captured in gas-phase decomposition, is the dominant driving force for this peptide. Specifically, residues F5, M7, L8, T9 contribute to binding via hydrophobic contacts that are invisible in the gas-phase per-residue energy. Alternative scoring approaches (e.g., total MM-GBSA with solvation, contact maps, or alanine scan experiments) should be used.

CAVEAT: This finding is limited to Hsc70 SBD (PDB 4PO2) and the TGGFQMALT peptide. It may not generalize to other peptide-receptor systems where polar or electrostatic interactions dominate. The gas-phase energies were computed from a single HADDOCK3-generated complex model (cluster 1, model 1).
