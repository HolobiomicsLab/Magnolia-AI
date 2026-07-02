---
confidence: 0.7
created: '2026-06-17T19:26:33.781590+00:00'
description: For the tight-binding CDK4-derived peptide TGGFQMALT (Ac-TGGFQMALT, Kd
  ~20 nM per Torielli 2025), per-residue gas-phase energy decomposition from MM-GBSA
  (Amber `MMPBSA.py`, gas=True) assigns the high
id: '20260617_192633_781576'
observation_count: 2
observed_in_sessions: []
source: auto_extraction
tags:
- MM-GBSA
- per-residue
- hydrophobic-effect
- TGGFQMALT
- Torielli
- ranking-failure
title: "MM-GBSA per-residue gas-phase energy cannot rank Hsc70 binding \u2014 TGGFQMALT\
  \ case"
tools:
- AmberTools
- MMPBSA.py
type: failure_pattern
updated: '2026-06-24T19:23:19.308592+00:00'
---

For the tight-binding CDK4-derived peptide TGGFQMALT (Ac-TGGFQMALT, Kd ~20 nM per Torielli 2025), per-residue gas-phase energy decomposition from MM-GBSA (Amber `MMPBSA.py`, gas=True) assigns the highest contribution to hydrophobic residues (e.g., Leu8, Met6, Phe4) but fails to reproduce the known binding affinity rank relative to other peptides. The solvation and entropy terms (PB/GB + ES) are essential for correct ranking; gas-phase energy alone is a poor predictor.

CAVEAT: This finding is specific to the Hsc70-peptide system (PDB 4PO2 SBD domain) and the MMPBSA.py gas-phase decomposition method. It may not generalize to other protein-ligand systems where electrostatic contributions dominate.

## Observation 2 (2026-06-24)

## Rationale
These scans were designed to test whether filling a putative hydrophobic pocket at position 3 (RKALQ) or position 2 (TGFMALQ) with larger hydrophobic residues (Val, Ile, Leu for A3; Ala for G2) would improve HADDOCK3 docking scores for the 5-mer peptide on the AF3 full-length Hsc70 receptor (closed state).

## Results
All six mutations produced HADDOCK3 scores (HADDOCK score, RMSD, cluster sizes) that were **worse or indistinguishable** from the wild‑type sequence. Specifically:
- RKALQ A3→V (RKVLQ), A3→I (RKILQ), A3→L (RKLLQ): all had higher (less negative) HADDOCK scores by ≥20 units compared to RKALQ.
- TGFMALQ G2→A (TAFMALQ): the mutation did not improve the HADDOCK score relative to TGFMALQ (Δscore < 5, within noise).
- No mutation created new stable clusters (all clustfcc clusters had n < 10 members) or altered the binding mode.

## Conclusion
The hydrophobic‑filling hypothesis does not hold for these specific positions/receptors. Other design strategies (e.g., polar or charge modifications, backbone extension) are likely required.

## CAVEAT
This finding applies only to:
- **Receptor**: AF3 full‑length Hsc70 (closed state, chain A).
- **Peptides**: 5‑mers with sequences starting KFERQ core (for RKALQ series) or the TGGFQMALT core (for TGFMALQ series).
- **Tool**: HADDOCK3 with default `it0` rigid‑body docking (no refinement).
- **Protocol**: Single‑point mutation scans without explicit water or co‑factor modeling.

Different receptors (e.g., 4PO2), longer peptides, or alternative docking protocols may yield different results.
