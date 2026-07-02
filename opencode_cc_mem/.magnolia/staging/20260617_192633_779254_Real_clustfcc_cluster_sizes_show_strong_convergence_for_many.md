---
confidence: 0.7
created: '2026-06-17T19:26:33.779282+00:00'
description: The n=4 cluster count reported in `capri_clt.tsv` for the 4PO2 SBD domain
  runs is an artifact of the `seletopclusts` `top_models=200` parameter, which caps
  the number of models fed to clustering. Actu
id: '20260617_192633_779254'
observation_count: 1
source: auto_extraction
tags:
- HADDOCK3
- clustfcc
- convergence
- seletopclusts
- 4po2
- cluster-size
title: Real clustfcc cluster sizes show strong convergence for many 4PO2 peptides
tools:
- HADDOCK3
type: parameter_guidance
updated: '2026-06-17T19:26:33.779282+00:00'
---

The n=4 cluster count reported in `capri_clt.tsv` for the 4PO2 SBD domain runs is an artifact of the `seletopclusts` `top_models=200` parameter, which caps the number of models fed to clustering. Actual `clustfcc.txt` cluster sizes (e.g., cluster 1 with 292/5000 models for peptide RKDLQ) indicate robust convergence for most peptides. This artifact does not reflect poor sampling.

CAVEAT: This observation applies only to the 4PO2 SBD domain (Hsc70) with the HADDOCK3 protocol using `topoaa/rigidbody/flexref/emref/seletopclusts` and 5000 models generated. Other systems or protocols may behave differently.
