---
confidence: 0.9
created: '2026-06-19T10:10:10.698103+02:00'
description: The fetch_job_results tool returns error_kind 'run_record_missing' with
  message 'project_dir is required' when called without the project_dir parameter.
  Observed with args_summary 'job_id=nope' and no
id: '20260619_101010_698103'
observation_count: 1
source: auto_extraction
tags:
- compchem-tools
- fetch_job_results
- parameter-requirements
title: fetch_job_results requires project_dir argument
tools:
- fetch_job_results
type: parameter_guidance
updated: '2026-06-19T10:10:10.698103+02:00'
---

The fetch_job_results tool returns error_kind 'run_record_missing' with message 'project_dir is required' when called without the project_dir parameter. Observed with args_summary 'job_id=nope' and no project_dir. The tool does not assume any default project directory; it must be explicitly provided.

CAVEAT: This applies only to the compchem-tools fetch_job_results tool. The error was triggered with a non-existent job_id and missing project_dir. In normal usage with a valid project_dir and a real job_id, the tool may behave differently.
