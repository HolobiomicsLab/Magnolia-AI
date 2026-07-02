---
confidence: 0.8
created: '2026-06-01T19:32:31.878929+00:00'
description: 'Symptoms: submit_job with scheduler=''ssh-slurm'' repeatedly raised
  TypeError: argument should be a str or an os.PathLike object where __fspath__ returns
  a str, not ''NoneType''.


  Cause: The submit_job to'
id: '20260601_193231_878900'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- submit_job
- ssh-slurm
- process-management
title: submit_job fails with TypeError when scheduler=ssh-slurm due to missing template_field
  handling
tools:
- submit_job
type: error_resolution
updated: '2026-06-01T19:32:31.878935+00:00'
---

Symptoms: submit_job with scheduler='ssh-slurm' repeatedly raised TypeError: argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'NoneType'.

Cause: The submit_job tool for 'ssh-slurm' scheduler requires a 'template_fields' argument (e.g. for job script substitution) which was not provided; it defaulted to None and the internal code path tried to use it as a file path.

Fix: Switch scheduler to 'local' and reduce ncores to an available count (e.g. 4) — job with scheduler='local', ncores=4 succeeded immediately.

Red herrings eliminated: The error was NOT due to wrong working_dir, missing binary, or conda path — all 6 attempts with ssh-slurm failed identically regardless of job_name or ncores. The local scheduler path works correctly.
