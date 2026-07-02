---
confidence: 0.9
created: '2026-06-17T19:26:33.784152+00:00'
description: 'Symptoms: When calling `check_job(job_id=11433514, scheduler=slurm,
  cluster=azzurra)`, the tool returned error "squeue/sacct binary not found on PATH".
  This occurred for all four job IDs checked (1143'
id: '20260617_192633_784144'
observation_count: 1
source: auto_extraction
tags:
- check_job
- Slurm
- azzurra
- tool-error
- dependency-missing
title: check_job tool fails on Azzurra due to missing Slurm binaries in client environment
tools:
- compchem-tools
type: failure_pattern
updated: '2026-06-17T19:26:33.784152+00:00'
---

Symptoms: When calling `check_job(job_id=11433514, scheduler=slurm, cluster=azzurra)`, the tool returned error "squeue/sacct binary not found on PATH". This occurred for all four job IDs checked (11433514–11433517).

Cause: The compchem-tools check_job function relies on local Slurm client commands (`squeue`, `sacct`) but these binaries are not installed in the agent's runtime environment. The cluster is Azzurra (remote Slurm system), but the tools attempt to query it via local Slurm commands rather than SSH or API.

Fix: Not resolved in this session. Likely solution: use the `remote_run` or SSH-based cluster tool to execute `squeue` on the login node, or install Slurm client tools (e.g., `slurm-client` package) in the agent environment.

Red herrings eliminated: The cluster address and scheduler type were correct; the issue was purely missing local dependencies.
