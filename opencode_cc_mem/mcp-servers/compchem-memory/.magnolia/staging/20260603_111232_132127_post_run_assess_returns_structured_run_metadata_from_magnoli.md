---
confidence: 0.9
created: '2026-06-03T11:12:32.132162+00:00'
description: The post_run_assess tool successfully retrieves structured assessment
  data (exit code, run directory, project path) from a Magnolia-managed xtb run. Calls
  across 7 different pytest temp projects all r
id: '20260603_111232_132127'
observation_count: 1
source: auto_extraction
tags:
- magnolia
- post_run_assess
- xtb
- run-management
title: post_run_assess returns structured run metadata from .magnolia/runs directory
tools:
- post_run_assess
type: success_pattern
updated: '2026-06-03T11:12:32.132168+00:00'
---

The post_run_assess tool successfully retrieves structured assessment data (exit code, run directory, project path) from a Magnolia-managed xtb run. Calls across 7 different pytest temp projects all returned valid JSON with identical structure: run_dir pointing to .magnolia/runs/xtb_demo, exit_code present as integer. Duration was consistently 12–28 ms. The tool correctly resolves the run path relative to a given project directory even when the path is provided without trailing slash.
