---
confidence: 0.7
created: '2026-06-01T19:32:31.882546+00:00'
description: 'Submitted haddock3 config.cfg with scheduler=''local'', ncores=4, job_name=''KILDQ_s1000''.
  The job started successfully (PID 90713). This demonstrated that:

  - haddock3 version 2025.11.0 is functional fro'
id: '20260601_193231_882523'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- hsc70
- KILDQ
- local-execution
- peptide-docking
title: haddock3 with local scheduler succeeds for KILDQ docking on hsc70
tools:
- submit_job
- haddock3_parse_results
type: success_pattern
updated: '2026-06-01T19:32:31.882551+00:00'
---

Submitted haddock3 config.cfg with scheduler='local', ncores=4, job_name='KILDQ_s1000'. The job started successfully (PID 90713). This demonstrated that:
- haddock3 version 2025.11.0 is functional from /home/tjiang/.../softwares/bin/haddock3
- The docking config.cfg in the 2026-06-01_KILDQ_s1000 directory is valid
- Running locally with limited cores avoids the ssh-slurm submission failure

CAVEAT: This worked for a single 5mer peptide (KILDQ) with full hsc70 receptor. Local execution time and scalability for multiple peptides or larger sampling (s5000) was not tested in this session — the s2000 and s5000 runs were submitted via ssh-slurm and their status could not be verified locally.
