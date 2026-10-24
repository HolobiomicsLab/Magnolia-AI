---
confidence: 0.9
created: '2026-06-19T11:50:58.957450+02:00'
description: 'The user implemented keyword splitting in memory_search. Tested with
  query ''entity13'' after the fix: the search now returns relevant results from both
  session and staging tiers (13 staging entries foun'
id: '20260619_115058_957450'
observation_count: 1
observed_in_sessions:
- ses_120fa984affeN2zTmXpUOp3es8
opencode_session_id: ses_120fa984affeN2zTmXpUOp3es8
source: opencode_distill
tags:
- memory_search
- tool_fix
- keyword_splitting
- multi_tier_search
title: memory_search now supports keyword splitting for multi-word queries
tools:
- memory_search
type: workflow_note
updated: '2026-06-19T11:50:58.957450+02:00'
---

The user implemented keyword splitting in memory_search. Tested with query 'entity13' after the fix: the search now returns relevant results from both session and staging tiers (13 staging entries found). Previously only self-referential session hits were returned. The tool correctly identifies entity13-related staging entries even when the model is mentioned in prose.
