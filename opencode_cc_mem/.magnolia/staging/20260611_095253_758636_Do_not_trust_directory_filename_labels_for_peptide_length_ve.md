---
confidence: 0.9
created: '2026-06-11T09:52:53.758668+00:00'
description: 'Symptoms: Agent assumed peptide length based on directory/filename labels
  (e.g., ''x2 10-mer'') without verification. This led to incorrect presentation of
  peptide lengths in design rationale.

  Cause: Fi'
id: '20260611_095253_758636'
observation_count: 1
source: auto_extraction
tags:
- peptide-lengths
- pdb-verification
- naming
- prejob_check
- hsc70_new
title: "Do not trust directory/filename labels for peptide length \u2014 verify PDB\
  \ residue counts"
tools: []
type: failure_pattern
updated: '2026-06-11T09:52:53.758674+00:00'
---

Symptoms: Agent assumed peptide length based on directory/filename labels (e.g., 'x2 10-mer') without verification. This led to incorrect presentation of peptide lengths in design rationale.
Cause: Filenames and directory names in the hsc70_new project (e.g., fusion_x2_10mer) may be legacy or inaccurate; they do not always reflect actual residue counts.
Fix: Always count residues from the PDB file before reporting length. Use: `grep '^ATOM' peptide.pdb | awk '{print $5}' | sort -u | tail -n1` or inspect residue numbers directly. In this session, the correct lengths were recorded in a correction entry (confirmed 2026-06-11).
Red herrings: None; the assumption itself was the sole source of error.
CAVEAT: This finding applies to the hsc70_new peptide docking project, where multiple fusion constructs (KFERQx2, RKELQx2, QRKELx2) were used. Always verify PDB residues for any peptide model, especially when names suggest a specific length.
