---
confidence: 0.55
created: '2026-06-19T10:21:08.528056+02:00'
description: 'In a test session, poll_jobs returned {''skipped'': ''busy''} on multiple
  calls (timestamps 07:22:12, 07:22:59, 07:23:48, 08:03:12, 08:03:50) while other
  calls returned zero counts (e.g., 08:05:54, 08:08:'
id: '20260619_102108_528056'
observation_count: 1
source: auto_extraction
tags:
- compchem-tools
- poll_jobs
- transient-behavior
title: 'poll_jobs returns ''skipped: busy'' as a transient condition'
tools:
- poll_jobs
type: workflow_note
updated: '2026-06-19T10:21:08.528056+02:00'
---

In a test session, poll_jobs returned {'skipped': 'busy'} on multiple calls (timestamps 07:22:12, 07:22:59, 07:23:48, 08:03:12, 08:03:50) while other calls returned zero counts (e.g., 08:05:54, 08:08:10, etc.). The 'busy' response is not an error but a transient condition indicating the tool is occupied. It should be handled by retrying after a short delay.
CAVEAT: Observed only in this automated test environment with dummy project dirs. Production behavior may differ; the condition may be tied to concurrent poll operations.
