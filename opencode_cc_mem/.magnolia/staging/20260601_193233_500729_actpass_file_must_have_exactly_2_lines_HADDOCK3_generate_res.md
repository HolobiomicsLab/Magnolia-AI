---
confidence: 0.95
created: '2026-06-01T19:32:33.500768+00:00'
description: 'Symptoms: generate_restraints tool failed with ''actpass file must have
  exactly 2 lines (got 1)''. This occurred when the actpass files derived from a fully
  unbound (no active residues defined) scenario'
id: '20260601_193233_500729'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- generate_restraints
- haddock3-restrain
- actpass-format
- error-resolution
title: "actpass file must have exactly 2 lines \u2014 HADDOCK3 generate_restraints\
  \ failure"
tools:
- haddock3
- generate_restraints
type: error_resolution
updated: '2026-06-01T19:32:33.500776+00:00'
---

Symptoms: generate_restraints tool failed with 'actpass file must have exactly 2 lines (got 1)'. This occurred when the actpass files derived from a fully unbound (no active residues defined) scenario.

Cause: The receptor.actpass and/or ligand.actpass files contained only 1 line instead of the required 2. An actpass file needs two lines: the first for active residues, the second for passive residues. When no residues were annotated as active (empty first line), haddock3-restrain produced a malformed file.

Fix: Run haddock3-restrain with properly formatted actpass files that have exactly 2 lines. Red herrings eliminated: The error is NOT caused by wrong file permissions, binary version, or missing PDB files.

CAVEAT: Applies to haddock3-restrain version shipped with haddock3-2025.11.0. The fix depends on how the actpass files are generated.
