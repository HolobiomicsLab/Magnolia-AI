---
author: auto
confidence: 0.95
created: '2026-06-16T07:11:34.132337+00:00'
date: '2026-06-16'
description: "## KB summary\n\nThe `Hsc70_peptide_binding` Perspicacit\xE9 KB now\
  \ contains 16 papers (127 chunks) covering Hsc70/Hsp70 peptide binding from two\
  \ complementary angles:\n\n### Structural & mechanistic (origin"
id: '20260616_071134_132311'
last_verified: '2026-06-16'
observation_count: 1
observed_in_sessions:
- 2026-06-16_062532
source: auto
tags:
- perspicacite
- literature
- hsc70
- CMA
- KFERQ
- 4PO2
- binding-site
- presentation
title: "Perspicacit\xE9 KB Hsc70_peptide_binding \u2014 16 papers covering both structural\
  \ biology and CMA degrader engineering"
tools: []
type: note
updated: '2026-06-16T07:11:34.134242+00:00'
---

## KB summary

The `Hsc70_peptide_binding` Perspicacité KB now contains 16 papers (127 chunks) covering Hsc70/Hsp70 peptide binding from two complementary angles:

### Structural & mechanistic (original Perspicacité crawl — 8 papers)
- Takeda & McKay 1996 — Kinetics of peptide binding to bovine Hsc70
- Azoulay et al. 2013 — DnaK lid domain dynamics with bound peptide
- Zhang et al. 2014 — Crystal structure of Hsp70 SBD + peptide (**PDB 4PO2** — our receptor template)
- Schneider et al. 2016 — BiPPred: sequence+structure prediction of Hsp70 binding
- Nordquist et al. 2022 — Review: computational modeling of Hsp70-client interactions
- Sahu & Malhotra 2025 — CMA substrate interactions with Hsc70 (AF3 predictions, GitHub: CSBLabNM/Project_HSC70)
- Torielli et al. 2025 — Multi-target peptide modulators for chaperone networks
- Bhasne et al. 2026 — Model peptides for Hsp70 binding mode comparison

### CMA degrader engineering (from experimentalist's presentation 2026-06-15 — 8 papers)
- Fan et al. 2014 (Nat Neurosci) — First peptide-directed lysosomal degrader
- Zhou et al. 2019 (Aging Dis) — CDK5 degradation for stroke
- Dou et al. 2020 (BBRC) — Aβ oligomer clearance via Hsc70
- Zhang et al. 2020 (Biocell) — CMATAC for ERα degradation
- Wang et al. 2024 (ACS Chem Biol) — SM-CMAD nanoparticle degrader
- Shao et al. 2024 (Angew Chem) — Ab-CMA antibody conjugate
- Song et al. 2024 (ACS Nano) — InCMATAC LNP degrader for STAT3
- Zhao et al. 2025 (J Nanobiotech) — CTAC-PDL for PD-L1

All CMA degrader papers use the KFERQ-KILDQ-RFFE targeting motif.

## Binding site comparison

- **Presentation (Sahu & Malhotra):** 8 residues: Ile403, Glu404, Phe428, Thr429, Thr430, Val438, Ile440, Ile474
- **Our 4PO2 analysis:** 19 tight-contact residues within 4 Å of NRLLLTG: 403, 404, 405, 406, 411, 426, 427, 428, 429, 430, 431, 435, 437, 438, 439, 440, 460, 469, 473
- The presentation uses AlphaFold3 predictions; we use actual x-ray crystal structure

## Key gap
The experimentalist is not aware of the 4PO2 crystal structure (Zhang et al. 2014, PDB 4PO2, 2.0 Å) — our docking receptor template is a higher-quality structural foundation than the AF3 predictions they rely on.

## CAVEAT
This KB covers peptide binding to Hsc70/Hsp70 SBD. It does NOT cover nucleotide-binding domain (NBD) functions, co-chaperone interactions (Hsp40, Bag, Hip, Hop), or the full Hsp70 allosteric cycle beyond what's needed to understand the open/closed conformational states of the SBD.
