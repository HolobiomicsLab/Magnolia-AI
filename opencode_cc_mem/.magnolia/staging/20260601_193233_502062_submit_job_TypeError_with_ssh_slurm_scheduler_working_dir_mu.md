---
confidence: 0.9
created: '2026-06-01T19:32:33.502087+00:00'
description: 'Symptoms: Calling submit_job with scheduler=ssh-slurm repeatedly fails
  with ''TypeError: argument should be a str or an os.PathLike object where __fspath__
  returns a str, not ''NoneType''''. Switching sch'
id: '20260601_193233_502062'
observation_count: 1
source: auto_extraction
tags:
- submit_job
- ssh-slurm
- bug-tool
- TypeError
- haddock3
- job-submission
title: "submit_job TypeError with ssh-slurm scheduler \u2014 working_dir must be path\
  \ string, not None"
tools:
- submit_job
type: error_resolution
updated: '2026-06-01T19:32:33.502093+00:00'
---

Symptoms: Calling submit_job with scheduler=ssh-slurm repeatedly fails with 'TypeError: argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'NoneType''. Switching scheduler to 'local' with the same command succeeds (job ID local_90713_7178e1).

Cause: The ssh-slurm scheduler logic tried to access a working_dir that was None. The submit_job tool's code has a bug: when the scheduler is 'ssh-slurm' and no explicit working_dir path is provided, it defaults to None instead of falling back to the current directory or requiring it.

Fix: Use scheduler=local instead for local execution. For ssh-slurm, ensure the working_dir argument is explicitly provided as a non-None string path. Red herrings eliminated: The error is NOT about missing sbatch/slurm binaries or cluster connectivity; it's purely a Python NoneType handling issue in the tool code.
