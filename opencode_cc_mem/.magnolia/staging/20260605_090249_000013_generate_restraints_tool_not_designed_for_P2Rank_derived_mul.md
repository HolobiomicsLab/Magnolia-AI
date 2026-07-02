---
confidence: 0.8
created: '2026-06-05T09:02:49.000026+00:00'
description: The `generate_restraints` utility enforces a strict 2-line actpass format.
  All 5 phosphorylated peptide runs (using P2Rank pocket 1) had multi-residue actpass
  files from the HADDOCK3 workflow setup, c
id: '20260605_090249_000013'
observation_count: 1
source: auto_extraction
tags:
- generate_restraints
- haddock3
- actpass-format
- p2rank
- failure
title: generate_restraints tool not designed for P2Rank-derived multi-residue actpass
  files
tools:
- generate_restraints
- stage_gate
type: failure_pattern
updated: '2026-06-05T09:02:49.000029+00:00'
---

The `generate_restraints` utility enforces a strict 2-line actpass format. All 5 phosphorylated peptide runs (using P2Rank pocket 1) had multi-residue actpass files from the HADDOCK3 workflow setup, causing 5 consecutive failures. The `stage_gate` tool confirmed all other inputs (config, receptor, ligand, restraints files) existed and were valid — the actpass format is the only broken step.

CAVEAT: This failure pattern applies only when actpass files contain >2 lines (i.e., more than one active residue and one passive residue line). Single-residue or correctly formatted 2-line actpass files work without issue. The P2Rank/HADDOCK3 workflow setup (using `haddock3-body`) generates multi-residue actpass differently than the `generate_restraints` parser expects.
