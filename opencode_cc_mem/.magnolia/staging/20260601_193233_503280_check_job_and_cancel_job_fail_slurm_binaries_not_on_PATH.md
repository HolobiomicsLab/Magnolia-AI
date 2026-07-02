---
confidence: 0.95
created: '2026-06-01T19:32:33.503307+00:00'
description: 'Symptoms: Both check_job (with scheduler=slurm, cluster=azzurra) and
  cancel_job (with scheduler=slurm) fail with ''squeue/sacct binary not found on PATH''
  or ''scancel binary not found on PATH''. Multiple'
id: '20260601_193233_503280'
observation_count: 1
source: auto_extraction
tags:
- slurm
- squeue
- sacct
- scancel
- missing-binary
- environment-limitation
- haddock3
title: 'check_job and cancel_job fail: slurm binaries not on PATH'
tools:
- check_job
- cancel_job
type: error_resolution
updated: '2026-06-01T19:32:33.503313+00:00'
---

Symptoms: Both check_job (with scheduler=slurm, cluster=azzurra) and cancel_job (with scheduler=slurm) fail with 'squeue/sacct binary not found on PATH' or 'scancel binary not found on PATH'. Multiple job IDs checked: 11335540, 11335541, 11335641-11335644.

Cause: The slurm CLI tools (squeue, sacct, scancel) are not installed or not present in the system PATH of the environment where the agent runs. The local execution environment does not have slurm client binaries.

Fix: Manually check job status on the cluster via SSH or use alternative cluster monitoring. The agent cannot query or cancel slurm jobs from this environment. Red herrings eliminated: The error is NOT about wrong job IDs, scheduler misconfiguration, or network issues.
