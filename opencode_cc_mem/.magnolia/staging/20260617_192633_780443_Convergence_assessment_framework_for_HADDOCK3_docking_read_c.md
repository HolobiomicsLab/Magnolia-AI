---
confidence: 0.7
created: '2026-06-17T19:26:33.780461+00:00'
description: HADDOCK3 has no formal convergence criterion. The best indicator is the
  cluster occupancy distribution in `clustfcc.txt` (number of models per cluster).
  The `capri_ss.tbl` file only reports scores for
id: '20260617_192633_780443'
observation_count: 1
source: auto_extraction
tags:
- HADDOCK3
- convergence
- clustfcc
- cluster-size
- assessment
title: "Convergence assessment framework for HADDOCK3 docking \u2014 read clustfcc.txt\
  \ not capri_ss.tbl"
tools:
- HADDOCK3
type: parameter_guidance
updated: '2026-06-17T19:26:33.780461+00:00'
---

HADDOCK3 has no formal convergence criterion. The best indicator is the cluster occupancy distribution in `clustfcc.txt` (number of models per cluster). The `capri_ss.tbl` file only reports scores for the top 10 models and does not capture sampling completeness. A well-converged run typically shows a single large cluster (>50% of models) and small satellite clusters.

CAVEAT: This framework is validated only for peptide-protein docking runs using the standard HADDOCK3 protocol (topoaa → rigidbody → flexref → emref → seletopclusts). For multi-body docking or other sampling strategies, different metrics may be needed.
