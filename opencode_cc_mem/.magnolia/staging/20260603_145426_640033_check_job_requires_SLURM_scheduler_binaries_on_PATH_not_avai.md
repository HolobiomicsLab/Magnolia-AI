---
confidence: 0.95
created: '2026-06-03T14:54:26.640056+00:00'
description: 'Attempts to check HADDOCK3 job status via `check_job(job_id=11340442,
  scheduler=''slurm'', cluster=''azzurra'')` failed with error: ''squeue/sacct binary
  not found on PATH''. Both job IDs 11340444 and 11340'
id: '20260603_145426_640033'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- slurm
- job-management
- azzurra
title: "check_job requires SLURM scheduler binaries on PATH \u2013 not available from\
  \ session context"
tools:
- check_job
- slurm
type: failure_pattern
updated: '2026-06-03T14:54:26.640060+00:00'
---

Attempts to check HADDOCK3 job status via `check_job(job_id=11340442, scheduler='slurm', cluster='azzurra')` failed with error: 'squeue/sacct binary not found on PATH'. Both job IDs 11340444 and 11340441 gave the same error. The agent cannot query SLURM job status remotely without either ssh access to the cluster or a web API wrapper. All SLURM-dependent status checks must be replaced by either (a) parsing run_<jobid>.out logs in the project directory, (b) using haddock3-specific completion indicators (e.g. `io.json` files), or (c) manual submission logs.
CAVEAT: This applies specifically to the azzurra cluster at this institution. Other clusters with squeak/sacct on the login node PATH may work.
