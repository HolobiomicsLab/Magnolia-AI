---
confidence: 0.9
created: '2026-06-05T09:02:40.347565+00:00'
description: 'Symptoms: `generate_restraints` tool returned `success: false` with
  error "/path/to/receptor.actpass: actpass file must have exactly 2 lines" for runs
  KFERQpK_p2rank1_s5000, KFERQpF_p2rank1_s5000, KFE'
id: '20260605_090240_347514'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- generate_restraints
- actpass-format
- hsc70
- error
title: generate_restraints fails on multi-line .actpass files
tools:
- generate_restraints
type: error_resolution
updated: '2026-06-05T09:02:40.347574+00:00'
---

Symptoms: `generate_restraints` tool returned `success: false` with error "/path/to/receptor.actpass: actpass file must have exactly 2 lines" for runs KFERQpK_p2rank1_s5000, KFERQpF_p2rank1_s5000, KFERQpQ_p2rank1_s5000, KFERQpA_C_p2rank1_s5000, ApKFERQ_N_p2rank1_s5000.

Cause: The receptor.actpass files contained more than 2 lines instead of the required 2-line format (active residues line + passive residues line).

Fix: Ensure actpass files are generated with exactly 2 lines using haddock3's expected format. The tool requires `segid_one=A` and `segid_two=B` which was passed correctly, but the input .actpass files from prior runs were malformed.

Red herrings eliminated: The segid parameters (A and B) are correct; the issue is the .actpass file content itself, not the tool arguments.
