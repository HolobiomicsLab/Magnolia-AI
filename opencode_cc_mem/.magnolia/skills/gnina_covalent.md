---
name: gnina_covalent
description: Gnina covalent docking workflow with alkyne warheads
version: 1.0
last_verified: 2026-03-31
tags: [gnina, covalent-docking, alkyne, vinyl]
---

# Gnina Covalent Docking

## Overview

Gnina supports covalent docking via the `--covalent_rec_res` and `--covalent_lig_atom_pattern` flags. This workflow handles alkyne-containing covalent warheads by converting them to Z/E vinyl isomers before docking.

## Workflow Steps

1. **Prepare receptor** — clean PDB, add chain IDs, remove waters
2. **Generate vinyl isomers** — `alkyne_to_vinyl` converts terminal alkynes to Z and E vinyls
3. **Validate SMARTS** — `smarts_validate` confirms the covalent atom pattern matches exactly 1 atom
4. **Run covalent docking** — `gnina_dock` with `--covalent_*` flags for each isomer
5. **Parse results** — `gnina_parse_results` extracts CNN scores and binding affinities

## Critical Rules

- **Always generate both Z and E isomers** — docking a single isomer misses valid binding modes
- **Validate SMARTS before docking** — incorrect SMARTS silently produces wrong results
- **Covalent receptor atom format**: chain:resnum:atomname (e.g., `A:45:SG` for cysteine)
- **Use `--seed` for reproducibility** in production runs

## Common Mistakes

- Forgetting to convert alkynes to vinyls before covalent docking
- Using a SMARTS that matches 0 or 2+ atoms instead of exactly 1
- Not checking that the receptor cysteine is not forming disulfide bonds
- Using wrong atom numbering after preprocessing

## Gnina Parameters

- `--autobox_ligand`: use reference ligand for box centering
- `--exhaustiveness 8`: minimum for covalent mode (default 1 is too low)
- `--num_modes 20`: generate enough poses for analysis
- `--cnn_scoring`: use CNN rescoring for better pose ranking

## Covalent Docking Commands

```bash
# Classical docking
gnina -r receptor.pdb -l ligand.sdf -o docked.sdf --autobox_ligand ref.pdb

# Covalent docking
gnina -r receptor.pdb -l ligand.sdf -o docked.sdf \
  --covalent_rec_res A:45:SG \
  --covalent_lig_atom_pattern "[C:1]=[C:2]" \
  --exhaustiveness 8
```

## Stage Gates

- `vinyl_isomers_exist` — checks Z and E isomer files are present
- `smarts_exactly_one_match` — validates SMARTS pattern on ligand
- `docked_poses_exist` — checks docking output files exist and are non-empty
