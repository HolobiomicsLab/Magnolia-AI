---
confidence: 0.9
created: '2026-06-05T09:02:48.998642+00:00'
description: When submitting SLURM jobs to the `cpucourt` partition on Azzurra, always
  request `--time=72:00:00` (72 hours). This is the maximum allowed walltime for this
  partition. Using shorter times may cause p
id: '20260605_090248_998620'
observation_count: 1
source: auto_extraction
tags:
- slurm
- azzurra
- cpucourt
- walltime
- haddock3
title: Always use max walltime (72h) for cpucourt partition jobs
tools:
- sbatch
- slurm
type: parameter_guidance
updated: '2026-06-05T09:02:48.998647+00:00'
---

When submitting SLURM jobs to the `cpucourt` partition on Azzurra, always request `--time=72:00:00` (72 hours). This is the maximum allowed walltime for this partition. Using shorter times may cause premature job termination for long-running HADDOCK3 runs. Created as a persistent rule entry on 2026-06-05.

CAVEAT: Applies specifically to the `cpucourt` partition on Azzurra HPC cluster. Other partitions (e.g., `gpucourt`, `standard`) may have different walltime limits. HADDOCK3 runs were the intended workload; other tools may finish faster.
