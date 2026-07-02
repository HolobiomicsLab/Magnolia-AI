---
confidence: 0.85
created: '2026-06-17T19:26:20.625919+00:00'
description: For HADDOCK3 docking runs on the Hsc70 SBD (PDB 4PO2), real cluster sizes
  from clustfcc.txt show strong convergence for many 5-mer peptides (large cluster
  sizes). The small cluster size (n=4) reported
id: '20260617_192620_625904'
observation_count: 1
source: auto_extraction
tags:
- HADDOCK3
- clustfcc
- convergence
- seletopclusts
- 4po2
- cluster-size
- artifact
title: "HADDOCK3 convergence assessment uses clustfcc.txt, not capri_clt.tsv \u2014\
  \ seletopclusts n=4 is artifact"
tools:
- haddock3:clustfcc
- haddock3:seletopclusts
type: success_pattern
updated: '2026-06-17T19:26:20.625919+00:00'
---

For HADDOCK3 docking runs on the Hsc70 SBD (PDB 4PO2), real cluster sizes from clustfcc.txt show strong convergence for many 5-mer peptides (large cluster sizes). The small cluster size (n=4) reported in capri_clt.tsv for some peptides is an artifact caused by `seletopclusts` `top_models` parameter limiting the number of models passed to clustering. The actual clustfcc clusters contain many more models, confirming good sampling convergence.

Exact protocol: read `clustfcc.txt` from the analysis directory (e.g., `run_dir/analysis/clustfcc.txt`). Do NOT rely on `capri_clt.tsv` cluster sizes for convergence assessment.

CAVEAT: This finding is based on HADDOCK3 runs for peptide-Hsc70 docking where `seletopclusts` was configured with a small `top_models` value (e.g., 20). For runs where `seletopclusts` passes all models or where `capri_clt.tsv` is generated differently, this artifact may not apply. Always cross-check with `clustfcc.txt` for real cluster membership counts.
