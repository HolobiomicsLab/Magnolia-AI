---
confidence: 1.0
created: '2026-06-19T11:15:56.410145+02:00'
description: 'Attempting to run memory_consolidate on tier=''staging'' fails with
  ''Unknown tier: staging. Use project or skill''. Staging entries must be processed
  via consolidation proposals (proposals.md) and manual'
id: '20260619_111556_410145'
observation_count: 1
observed_in_sessions:
- ses_120fa984affeN2zTmXpUOp3es8
opencode_session_id: ses_120fa984affeN2zTmXpUOp3es8
source: opencode_distill
tags:
- API
- memory_consolidate
- memory_tool
- parameter-error
- staging_management
- tier
- tool_limitation
title: Memory_consolidate tool only works on project and skill tiers, not staging
tools:
- memory_apply_consolidation
- memory_consolidate
- memory_scan_headers
type: workflow_note
updated: '2026-06-19T11:15:56.410145+02:00'
---

Attempting to run memory_consolidate on tier='staging' fails with 'Unknown tier: staging. Use project or skill'. Staging entries must be processed via consolidation proposals (proposals.md) and manually applied with memory_apply_consolidation. Direct consolidation of staging is not supported.

## Corroborating observations (merged)

- (session ?) memory_scan_headers only accepts tier 'project' or 'skill'

Calling memory_scan_headers with tier='staging' returns error: 'Unknown tier: staging. Use 'project' or 'skill'.' Only these two tiers are valid. CAVEAT: Applies to current memory tool implementation; may change in future versions.
