---
confidence: 0.85
created: '2026-06-01T19:32:33.505431+00:00'
description: 'Parameters that produced successful runs for Hsc70 SBD + KILDQ 5-mer
  peptide:

  - Scheduler: local (fallback when ssh-slurm unavailable)

  - Ncores: 4 (max for local)

  - Module order: topoaa, rigidbody, se'
id: '20260601_193233_505412'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- successful-run
- KILDQ
- Hsc70-SBD
- peptide-docking
- caprieval
- rigidbody-docking
title: HADDOCK3 run configuration for Hsc70 SBD + KILDQ peptide yields successful
  CAPRI evaluation
tools:
- haddock3
- submit_job
- haddock3_parse_results
type: success_pattern
updated: '2026-06-01T19:32:33.505436+00:00'
---

Parameters that produced successful runs for Hsc70 SBD + KILDQ 5-mer peptide:
- Scheduler: local (fallback when ssh-slurm unavailable)
- Ncores: 4 (max for local)
- Module order: topoaa, rigidbody, seletop, caprieval
- Run naming convention: {peptide}_{SBD_flag}_s{Nsamples} (e.g. KILDQ_SBD_s1000)
- Input structures: Hsc70 SBD (chain A, 4792 atoms) and KILDQ (chain B, 53 atoms)
- Restraints: 90-91 from actpass-based generation
- All runs passed all modules (topoaa, rigidbody, seletop, caprieval) and produced CAPRI evaluation metrics.

CAVEAT: Results apply only to Hsc70 SBD domain (not full-length protein). Only tested with KILDQ peptide in SBD binding mode. Local scheduler limits throughput — does not scale to large sampling or multiple peptides without SLURM.
