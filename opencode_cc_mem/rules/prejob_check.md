---
name: prejob_check
description: Mandatory input verification checklist to run before every submit_job call. Catch mismatches that would waste a full Slurm allocation.
version: 1.0
last_verified: 2026-06-02
tags: [submit, validation, preflight, quality]
---

# Pre-Job Input Verification

**Never call `submit_job` without first running the checks in this rule.**
A job that fails 90 minutes in because residue numbers don't match between the
config and the PDB wastes cluster allocation, queue time, and the user's
attention.

## Universal checks (every tool)

1. **All input files present** — `ls` the run directory and confirm every file
   referenced in the config/command exists and is non-empty (`wc -c` > 0).
2. **Structure files validate** — call `validate_structure` on every PDB/SDF.
   Failures here mean a corrupted or truncated file.
3. **No symlinks to outside the run dir** — HADDOCK3 and other tools require
   self-contained directories. Every file must be a real copy, not a symlink
   pointing elsewhere.
4. **Config file parses** — if the tool has a config (cfg, yaml, mdp), confirm
   the syntax is valid. For HADDOCK3, confirm `run_dir = "output"` is relative.

## HADDOCK3-specific checks

1. **PDB segments and chains** — `grep '^ATOM' peptide.pdb | head -1` and
   `grep '^ATOM' receptor.pdb | head -1` to verify chain ID (column 22) and
   segment ID (columns 73-76) match what the config expects.
2. **Residue range consistency** — the `fle_sta_2 / fle_end_2` values in the
   config must match the actual first and last residue numbers in the peptide
   PDB. Verify with:
   ```bash
   grep '^ATOM' peptide.pdb | awk '{print $5,$6}' | sort -nu | head -1
   grep '^ATOM' peptide.pdb | awk '{print $5,$6}' | sort -nu | tail -1
   ```
3. **Actpass ↔ config agreement** — `peptide.actpass` residue numbers must match
   `fle_sta_2 / fle_end_2` in the config AND the actual PDB contents.
4. **Actpass format** — exactly 2 lines (active, passive), space-separated
   residue numbers. One-line actpass files (active-only, no passive) will fail
   in `generate_restraints` but are accepted by HADDOCK3 directly.
5. **Ambig.tbl consistency** — if copying an ambig.tbl from a previous run,
   verify it uses the correct residue numbers by grepping for `resi <N>` and
   comparing against the actpass.
6. **Scoring weights** — confirm `weights_params.json` (if present) or config
   module parameters match the intended scoring scheme.

## After verification passes

Only then call `submit_job`. If any check fails, surface the specific mismatch
to the user — do not submit.

## Common mismatches this rule catches

| Mistake | Check that catches it |
|---|---|
| Peptide renumbered but config not updated | Residue range consistency |
| Copied ambig.tbl from different peptide | Ambig.tbl consistency |
| Corrupt PDB from truncated rsync | Structure files validate |
| Missing segment ID in PDB column 73-76 | PDB segments and chains |
| Wrong chain/segid for receptor | PDB segments and chains |
| fle_end wrong for 6-mer vs 5-mer | Residue range consistency |
