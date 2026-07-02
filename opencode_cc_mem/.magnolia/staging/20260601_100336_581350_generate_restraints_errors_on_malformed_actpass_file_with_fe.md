---
confidence: 0.9
created: '2026-06-01T10:03:36.581390+00:00'
description: 'Symptoms: generate_restraints failed with error: "actpass file must
  have exactly 2 lines (got 1)". This occurred for peptide KILDQ''s receptor.actpass
  file located under runs/2026-06-01_KILDQ/.


  Cause:'
id: '20260601_100336_581350'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- haddock3-restrain
- actpass-format
- generate_restraints
- ambiguous-restraints
- ground_state
- opencode_cc_mem
title: generate_restraints errors on malformed actpass file with fewer than 2 lines
tools:
- generate_restraints
type: error_resolution
updated: '2026-06-01T10:03:36.581401+00:00'
---

Symptoms: generate_restraints failed with error: "actpass file must have exactly 2 lines (got 1)". This occurred for peptide KILDQ's receptor.actpass file located under runs/2026-06-01_KILDQ/.

Cause: The actpass file for the receptor only had 1 line of active/passive residue definitions instead of the required 2 lines (one for receptor, one for ligand). The file was likely auto-generated or truncated.

Fix: After providing properly formatted actpass files (manually prepared with 2 lines, one for chain A and one for chain B), generate_restraints succeeded, producing ambig.tbl files with 13-14 distance restraints (13 for KILDQ, KFERQ, QRKEL, RKELQ; 14 for RKDLQ).

Red herrings eliminated: The error is not related to the peptide identity or segid assignment — it's purely a file formatting issue specific to the actpass input.
