---
confidence: 0.95
created: '2026-06-01T09:57:32.379407+00:00'
description: 'After fixing actpass file format to have 2 lines (active + passive residues),
  the number of generated restraints increased dramatically for all peptides tested:
  KILDQ: 90, KFERQ: 90, QRKEL: 90, RKDLQ:'
id: '20260601_095732_379379'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- generate_restraints
- hsc70_peptides
- restraint-count
- actpass-format
- KILDQ
- KFERQ
- QRKEL
- RKDLQ
- RKELQ
- peptide-design
title: Restraint count jumps from 13-14 to 90-91 after formatting fix
tools:
- generate_restraints
type: parameter_guidance
updated: '2026-06-01T09:57:32.379412+00:00'
---

After fixing actpass file format to have 2 lines (active + passive residues), the number of generated restraints increased dramatically for all peptides tested: KILDQ: 90, KFERQ: 90, QRKEL: 90, RKDLQ: 91, RKELQ: 90. Before the fix, the same peptides generated only 13-14 restraints (RKDLQ: 14, others: 13). The restraint sets are structurally plausible: e.g., KILDQ (restraint_count=90) vs QRKEL (restraint_count=90) both achieved similar counts. RKDLQ had 91 restraints (one more than the others at 13-14 vs 13 before), suggesting RKDLQ may have one additional active or passive contact. The haddock3-restrain command used was standard with segid_one=A, segid_two=B.

CAVEAT: These counts are for 5mer peptides (sequences KILDQ, KFERQ, QRKEL, RKDLQ, RKELQ) docked to Hsc70 receptor (4792 atoms, chain A). The 90-residue-scale counts are for the corrected 2-line actpass format; the 13-14 counts were for the single-line actpass format and are not recommended. Applicability to other peptide lengths or different receptors is not established.
