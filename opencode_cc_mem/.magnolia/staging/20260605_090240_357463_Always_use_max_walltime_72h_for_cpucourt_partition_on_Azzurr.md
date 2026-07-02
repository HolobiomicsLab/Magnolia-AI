---
confidence: 0.9
created: '2026-06-05T09:02:40.357505+00:00'
description: 'When submitting Slurm jobs to the cpucourt partition on Azzurra, always
  request `--time=72:00:00`. This parameter was recorded as a rule for successful
  job completion.


  CAVEAT: Applies specifically to'
id: '20260605_090240_357463'
observation_count: 1
source: auto_extraction
tags:
- slurm
- azzurra
- cpucourt
- walltime
- sbatch
- parameter
title: Always use max walltime 72h for cpucourt partition on Azzurra
tools: []
type: parameter_guidance
updated: '2026-06-05T09:02:40.357515+00:00'
---

When submitting Slurm jobs to the cpucourt partition on Azzurra, always request `--time=72:00:00`. This parameter was recorded as a rule for successful job completion.

CAVEAT: Applies specifically to the cpucourt partition on Azzurra cluster only. Other partitions (e.g., gpu, debug) will have different walltime limits.
