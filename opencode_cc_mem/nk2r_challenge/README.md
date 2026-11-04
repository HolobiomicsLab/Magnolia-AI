# NK2R peptide-design challenge — project scaffold

**Status: PROVISIONAL v0.1.** Attempts the NK2R design task from the FRCBS
(Tsinghua) Peptide Design Competition (Science/AAAS feature), reusing hsc70
peptide-docking machinery. All choices subject to change.

## What runs today

The cheap end of the funnel, deterministic and stdlib-only:

```sh
cd opencode_cc_mem
python3 nk2r_challenge/src/nk2r_funnel.py run \
    --seed 20261103 --per-generator 120 --target-n 60 \
    --out nk2r_challenge/runs/$(date +%Y-%m-%d)_v0.1
```

Outputs in the run dir: `run-record.json`, `ranked_candidates.csv` (60 rows),
`calibration.json`, `kill-ledger.json`. Exit code 0 only if every pre-registered
acceptance item passes (3 generators, none >50%, calibration passed, 60 ranked).

Tests:

```sh
python3 -m pytest tests/test_nk2r_funnel.py -q
```

## What is gated (not implemented)

- `dock_prepare` / `dock`: need NK2R structure + active-site definition +
  positive-control calibration + real Slurm acceptance test.
- `selectivity`: NK1R/NK3R structures.
- `package`: competition submission format unknown (see docs/campaign-brief.md).

## Reading the output honestly

The ranking heuristic is a tachykinin-motif sanity instrument, not a binding
model. Calibration proves the funnel ranks real tachykinin pharmacophores above
decoys — nothing about NK2R affinity or agonism. Any binding claim must come
from the gated docking stage, and activity (EC50) only from experiment.
