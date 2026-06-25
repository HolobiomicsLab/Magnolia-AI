---
name: p2rank
description: P2Rank pocket prediction: parameters, output formats, and best practices.
version: 1.0
last_verified: 2026-04-01
tags: [p2rank, pocket-prediction, binding-site]
---

# P2Rank Rules

## Overview

P2Rank is a stand-alone pocket prediction tool that uses machine learning to rank protein binding sites. It takes a PDB structure and outputs ranked pocket predictions as CSV files.

## Essential Commands

```bash
p2rank predict -f protein.pdb -o output_dir -threads 4
p2rank predict -f protein.pdb -o output_dir -c config.groovy
```

## Input Requirements

- Protein must be a valid PDB file
- Chain IDs are recommended but not required
- Remove waters and heteroatoms for best results (unless they define the binding site)
- Structure should be complete (no large missing loops)

## Output Files

| File | Description |
|------|-------------|
| `<stem>_predictions.csv` | Ranked pocket predictions with scores |
| `<stem>_pockets.pdb` | Pocket regions as PDB ATOM entries |

## Predictions CSV Columns

| Column | Type | Description |
|--------|------|-------------|
| name | string | Pocket identifier |
| rank | int | Ranking by score |
| score | float | Pocket probability score (higher is better) |
| probability | float | Predicted probability of being a druggable site |
| center_x/y/z | float | Pocket center coordinates |
| residue_count | int | Number of residues in pocket |
| residues | string | Space-separated residue identifiers |
| volume | float | Estimated pocket volume |

## Parameters

- `-threads N`: Number of threads (default 1, recommend 4)
- `-c config.groovy`: Custom configuration file
- `-model_dir`: Path to alternative ML model

## Common Mistakes

| Mistake | Correct |
|---------|---------|
| Running on a structure with missing chains | Preprocess PDB first |
| Expecting exact residue numbering | P2Rank may renumber residues |
| Using output directly without checking scores | Filter by score > 0.5 for high-confidence pockets |
| Not checking residue count | Pockets with <3 residues are often artifacts |

## Integration with Docking

1. Run P2Rank on receptor structure
2. Use top-ranked pocket center as docking box center
3. Use pocket residues to define active residues for HADDOCK restraints
4. Gate: `at_least_one_pocket` checks for valid predictions

## Quality Indicators

- Top pocket score > 0.7: high confidence
- Score 0.3-0.7: moderate, verify with known binding sites
- Score < 0.3: low confidence, consider alternative methods
- Multiple high-scoring pockets: investigate all
