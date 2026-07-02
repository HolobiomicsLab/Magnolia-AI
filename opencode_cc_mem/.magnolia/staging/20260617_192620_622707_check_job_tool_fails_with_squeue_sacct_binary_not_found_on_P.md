---
confidence: 0.95
created: '2026-06-17T19:26:20.622751+00:00'
description: 'Symptoms: The compchem-tools check_job function returned {"success":
  false, "error": "squeue/sacct binary not found on PATH"} for all four SLURM job
  IDs (11433514, 11433515, 11433516, 11433517) on the'
id: '20260617_192620_622707'
observation_count: 1
source: auto_extraction
tags:
- compchem-tools
- check_job
- Azzurra
- SLURM
- squeue
- sacct
- infrastructure
title: check_job tool fails with 'squeue/sacct binary not found on PATH' on Azzurra
tools:
- compchem-tools:check_job
type: error_resolution
updated: '2026-06-17T19:26:20.622751+00:00'
---

Symptoms: The compchem-tools check_job function returned {"success": false, "error": "squeue/sacct binary not found on PATH"} for all four SLURM job IDs (11433514, 11433515, 11433516, 11433517) on the Azzurra cluster.

Cause: The tool attempts to query job status via `squeue` or `sacct`, but these binaries are not available in the environment's PATH. This typically occurs when the tool runs on a machine that is not a SLURM login node or compute node (e.g., a head node or container without Slurm client tools installed).

Fix: Run the tool from a SLURM login node or a node where `squeue` and `sacct` are installed and on PATH. Alternatively, ssh directly to a cluster node and manually run `squeue -u $USER` or `sacct -j <job_id>` to check job status.

Red herrings eliminated: The error is not due to invalid job IDs, expired credentials, or incorrect cluster name — all job IDs were valid (11433514-11433517) and cluster was correctly set to 'azzurra'. The issue is purely missing binaries in the execution environment.

CAVEAT: This finding applies only to the compchem-tools check_job tool when running from a non-SLURM-client environment. On properly configured login nodes, the tool should work.
