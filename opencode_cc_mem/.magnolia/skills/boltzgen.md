---
name: boltzgen
version: 1.0
description: Practical guide for running BoltzGen protein design workflows, managing runs, and handling interruptions.
last_verified: 2026-04-16
---

# BoltzGen Practical Guide

## Installation

BoltzGen is installed in an isolated `uv` virtual environment at `softwares/boltzgen/`.

```bash
# Run via wrapper (preferred - sets up CUDA libraries)
softwares/boltzgen/bin/boltzgen [command]

# Or via magnolia-run for logging
magnolia-run softwares/boltzgen/bin/boltzgen run design.yaml --output runs/test
```

## Basic Run

```bash
boltzgen run design_spec.yaml --output runs/NAME --num_designs 20 --inverse_fold_num_sequences 10 --budget 10
```

**Key parameters:**
- `--num_designs`: Number of backbone structures to generate (design step)
- `--inverse_fold_num_sequences`: Sequences per backbone (typically 10)
- `--budget`: Final number of designs after filtering
- `--protocol`: `peptide-anything` for peptide design
- `--reuse`: Skip steps if outputs already exist

## Pipeline Steps

BoltzGen runs 6 steps in order:
1. **design** - Generate backbone structures
2. **inverse_folding** - Design sequences for each backbone  
3. **folding** - Fold sequences, compute confidence metrics
4. **design_folding** - Optional: additional folding analysis
5. **analysis** - Compute metrics (SASA, H-bonds, liabilities)
6. **filtering** - Rank and select best designs

## Critical: Restarting Interrupted Runs

**ALWAYS use `--reuse` when resuming:**

```bash
boltzgen run design.yaml --output runs/NAME --reuse --steps folding analysis filtering
```

**CRITICAL: Set `skip_existing=true` to avoid reprocessing:**

```bash
boltzgen run design.yaml --output runs/NAME --reuse \
  --steps folding analysis filtering \
  --config folding data.skip_existing=true
```

**Without this, BoltzGen will re-run ALL folding steps, even completed ones!**

### How Resumption Works

- Each step creates outputs in `intermediate_designs_inverse_folded/`
- Folding outputs: `fold_out_npz/*.npz` and `refold_cif/*.cif`
- With `skip_existing=true`, BoltzGen checks for existing outputs and skips them

**Check progress:**
```bash
# Count completed fold outputs
ls runs/NAME/intermediate_designs_inverse_folded/fold_out_npz/ | wc -l

# Should reach num_designs × inverse_fold_num_sequences (e.g., 20 × 10 = 200)
```

## File Structure

```
runs/NAME/
├── config/                    # Generated step configs (YAML)
├── design_*.cif              # Final design specification
├── intermediate_designs/      # Step 1: Backbone structures
│   ├── design_*.cif
│   └── design_*.npz
├── intermediate_designs_inverse_folded/  # Steps 2-3
│   ├── design_*_*.cif        # Sequences (step 2)
│   ├── design_*_*.npz
│   ├── fold_out_npz/         # Folding outputs (step 3)
│   ├── refold_cif/           # Refolded structures
│   └── metrics_tmp/          # Analysis outputs (step 5)
└── final_ranked_designs/     # Step 6: Final results
    ├── all_designs_metrics.csv
    ├── final_N_designs/
    └── results_overview.pdf
```

## Common Issues

### 1. Analysis fails: "assert len(sample_ids) > 0"

**Cause:** Analysis step expects metrics in `metrics_tmp/` but folding didn't save them.

**Fix:** Ensure folding completed successfully. Check:
```bash
ls intermediate_designs_inverse_folded/metrics_tmp/
# Should have data_*.npz and metrics_*.npz files
```

### 2. Folding step hangs or is very slow

**Normal:** Folding takes ~80 seconds per design on A6000.
- 200 designs × 80s = ~4.5 hours

**Not normal:** If no progress after 5 minutes:
- Check GPU: `nvidia-smi`
- Check disk space

### 3. "No output directory specified. Exiting."

**Cause:** Running from inside the run directory.

**Fix:** Run from project root:
```bash
cd projects/NAME
boltzgen run design.yaml --output runs/RUN_NAME ...
```

### 4. Job crashes, can't resume

**If resume fails with existing configs:**
```bash
# Backup current run
cp -r runs/NAME runs/NAME_backup_$(date +%Y%m%d)

# Clean up and restart with --reuse
rm -rf runs/NAME/config/
boltzgen run design.yaml --output runs/NAME --reuse --steps [remaining_steps]
```

## Design Specification YAML

```yaml
version: 1
sequences:
  - name: target
    sequence: MTEYKLVVVGAGGVGKSALTIQLIQNHFVDEYDPTIEDSYRKQVVIDGETCLLDILDTAGQEEYSAMRDQYMRTGEGFLCVFAINNTKSFEDIHQYREQIKRVKDSDDVPMVLVGNKCDLAARTVESRQAQDLARSYGIPYIETSAKTRQGVEDAFYTLVREIRQHKLRKLNPPDESGPGCMSCKCVLS
    type: protein
  - name: binder
    length: 5
    type: protein
    constraints:
      - position: 1
        residue: LYS
      - position: 2
        residue: PHE
      - position: 3
        residue: GLU
```

**Key fields:**
- `length`: Binder length (e.g., 5 for 5-mer)
- `constraints`: Fix specific residues (e.g., K-F-E at positions 1-3)

## Resource Requirements

| Step | GPU | CPU | Time (200 designs) |
|------|-----|-----|-------------------|
| design | Yes | Low | ~20 min |
| inverse_folding | Yes | Low | ~3 min |
| folding | Yes | Low | ~4 hours |
| analysis | No | High (32 cores) | ~1-2 min |
| filtering | No | Low | ~20 sec |

## Workflow Examples

### Full run:
```bash
boltzgen run design.yaml --output runs/test \
  --num_designs 20 --inverse_fold_num_sequences 10 --budget 10
```

### Resume after crash (folding incomplete):
```bash
boltzgen run design.yaml --output runs/test --reuse \
  --steps folding analysis filtering \
  --config folding data.skip_existing=true
```

### Restart from scratch (keep backup):
```bash
cp -r runs/test runs/test_backup
rm -rf runs/test
boltzgen run design.yaml --output runs/test ...
```

## Verification Checklist

After completion, verify:
- [ ] `final_ranked_designs/all_designs_metrics.csv` exists
- [ ] Number of designs matches budget (or close)
- [ ] Top designs have reasonable scores (iptm > 0.6, pae < 10)
- [ ] No all-X sequences (check designed_sequence column)
