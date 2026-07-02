---
confidence: 0.95
created: '2026-06-01T19:32:31.879923+00:00'
description: "Symptoms: check_job (for slurm jobs 11335540\u201311335643) failed with\
  \ 'squeue/sacct binary not found on PATH'. Cancel_job failed with 'scancel binary\
  \ not found on PATH'.\n\nCause: The compute environment i"
id: '20260601_193231_879901'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- slurm
- check_job
- cancel_job
- ssh-slurm
- process-management
title: check_job and cancel_job fail because slurm binaries not on local PATH
tools:
- check_job
- cancel_job
- submit_job
type: error_resolution
updated: '2026-06-01T19:32:31.879928+00:00'
---

Symptoms: check_job (for slurm jobs 11335540–11335643) failed with 'squeue/sacct binary not found on PATH'. Cancel_job failed with 'scancel binary not found on PATH'.

Cause: The compute environment is on a remote cluster (azzurra) but the local dev machine does not have slurm command-line tools installed. These tools require executing on the cluster head node, not locally.

Fix: None available. This environment cannot manage remote slurm jobs — status must be checked via SSH to the cluster head node directly, or by inspecting run output directories for completeness flags.

Red herrings eliminated: The error is NOT about missing permissions, invalid job IDs, or wrong SLURM_* environment variables.
