---
confidence: 0.95
created: '2026-06-13T10:29:09.164353+00:00'
description: "entity8-KILDQ-fusion (14-mer) has 3 backbone breaks at the C-terminal\
  \ tail: CA-CA distances ARG857\u2192PHE658 = 6.0\u201313.6 \xC5, PHE658\u2192PHE659\
  \ = 5.1\u20138.8 \xC5, PHE659\u2192GLU660 = 5.9\u201337.9 \xC5 vs. normal 3.8 \xC5\
  . QRKELx2 (1"
id: '20260613_102909_163755'
observation_count: 1
opencode_session_id: ses_13fc23de2ffelf2ddyTWl2olKC
source: opencode_distill
tags:
- peptide_backbone_breaks
- entity8-KILDQ-fusion
- QRKELx2
- tleap_artifact
- HADDOCK3_s10000_run
- score_validation
title: Two longer input peptides have broken backbones, invalidating docking scores
tools:
- tleap
- xtb_optimize
- custom_pdb_analysis
type: scientific_finding
updated: '2026-06-13T10:29:09.164353+00:00'
---

entity8-KILDQ-fusion (14-mer) has 3 backbone breaks at the C-terminal tail: CA-CA distances ARG857→PHE658 = 6.0–13.6 Å, PHE658→PHE659 = 5.1–8.8 Å, PHE659→GLU660 = 5.9–37.9 Å vs. normal 3.8 Å. QRKELx2 (10-mer) has 1 break at N-terminus: GLN647→ARG648 CA-CA = 8.6 Å (input) worsened to 12.3 Å after docking. These breaks originate from tleap build errors, not docking. All other s5000 and s10000 peptide inputs (5–7‑mer, entity9, entity10‑variant, entity12) are clean. The −159.09 AIR‑free score for the 14‑mer fusion is unreliable because the peptide conformation is non‑physical.
