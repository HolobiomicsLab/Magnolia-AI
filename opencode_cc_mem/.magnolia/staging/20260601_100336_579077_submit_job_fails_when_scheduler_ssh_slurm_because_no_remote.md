---
confidence: 0.9
created: '2026-06-01T10:03:36.579137+00:00'
description: 'Symptoms: Repeated TypeError on submit_job calls: "argument should be
  a str or an os.PathLike object where __fspath__ returns a str, not ''NoneType''".
  This occurred with scheduler=ssh-slurm for command'
id: '20260601_100336_579077'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- submit_job
- ssh-slurm
- local-scheduler
- openproject
- opencode_cc_mem
- TypeError
- cluster-path-configuration
title: submit_job fails when scheduler=ssh-slurm because no remote cluster path is
  set
tools:
- submit_job
type: error_resolution
updated: '2026-06-01T10:03:36.579152+00:00'
---

Symptoms: Repeated TypeError on submit_job calls: "argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'NoneType'". This occurred with scheduler=ssh-slurm for commands running haddock3 config.cfg.

Cause: The submit_job tool with scheduler=ssh-slurm requires a remote working_dir path (the cluster-side directory). When working_dir is provided as a local path, the internal logic tries to convert None (the unset remote path) to a Path object, causing a TypeError.

Fix: Switch to scheduler=local, which accepts local paths directly. This immediately worked (job_id local_90713_7178e1, PID 90713, ncores=4).

Red herrings eliminated: Job name length (KILDQ_s1000 vs KILDQ_s2000 vs KILDQ_s5000) and ncores value (32 vs 4) are not the cause — the error is identical regardless.
