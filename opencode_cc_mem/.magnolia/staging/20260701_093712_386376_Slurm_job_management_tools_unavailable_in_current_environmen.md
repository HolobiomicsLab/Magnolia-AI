---
confidence: 0.9
created: '2026-07-01T09:37:12.386376+02:00'
description: "Symptoms: Consecutive calls to check_job (job IDs 11446731, 11446736,\
  \ 11453859\u201311453863) all returned error \"squeue/sacct binary not found on\
  \ PATH\". Likewise, cancel_job calls for the same job IDs ret"
id: '20260701_093712_386376'
observation_count: 1
source: auto_extraction
tags:
- compchem-tools
- check_job
- cancel_job
- slurm
- environment-setup
title: Slurm job management tools unavailable in current environment
tools:
- check_job
- cancel_job
type: note
updated: '2026-07-01T09:37:12.386376+02:00'
---

Symptoms: Consecutive calls to check_job (job IDs 11446731, 11446736, 11453859–11453863) all returned error "squeue/sacct binary not found on PATH". Likewise, cancel_job calls for the same job IDs returned "scancel binary not found on PATH". Cause: The environment (cluster=azzurra, host where the agent executes) does not have the Slurm command-line utilities (squeue, sacct, scancel) installed or accessible via PATH. No fix was applied during this session.

CAVEAT: This finding is bounded to the agent's execution environment as of 2026-07-01. It does not reflect a problem with the remote Slurm cluster itself; the agent simply lacks the binary paths to interact with it. Any computation launched via `submit_job` would still run on the cluster, but monitoring and cancellation from the agent are blocked.
