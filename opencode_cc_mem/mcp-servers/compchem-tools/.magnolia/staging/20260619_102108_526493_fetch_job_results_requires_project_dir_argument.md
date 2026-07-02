---
confidence: 0.65
created: '2026-06-19T10:21:08.526493+02:00'
description: 'Symptoms: fetch_job_results called with job_id=''nope'' but no project_dir.
  Returned success: false, error_kind: ''run_record_missing'', error: ''project_dir
  is required''.

  Cause: The tool requires the proj'
id: '20260619_102108_526493'
observation_count: 1
source: auto_extraction
tags:
- compchem-tools
- fetch_job_results
- parameter-requirement
title: fetch_job_results requires project_dir argument
tools:
- fetch_job_results
type: error_resolution
updated: '2026-06-19T10:21:08.526493+02:00'
---

Symptoms: fetch_job_results called with job_id='nope' but no project_dir. Returned success: false, error_kind: 'run_record_missing', error: 'project_dir is required'.
Cause: The tool requires the project_dir argument to locate the job record; job_id alone is insufficient.
Fix: Always provide the project_dir argument when calling fetch_job_results (e.g., project_dir='/path/to/project').
Also: The error is NOT due to an invalid job_id; the job_id 'nope' was never processed because the project_dir was missing. Providing a correct project_dir resolves the error even if the job_id is unrecognized.
