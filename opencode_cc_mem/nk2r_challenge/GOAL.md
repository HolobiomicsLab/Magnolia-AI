# Goal: nk2r_challenge

## One-line summary

Attempt the FRCBS (Tsinghua) Peptide Design Competition task on NK2R with an
auditable, calibrated peptide-design funnel, reusing hsc70 docking machinery.

## Inputs

- Science/AAAS feature "The peptide design challenge" (target NK2R; rules not included)
- hsc70_new project: HADDOCK3 peptide-docking pipeline, peptide builder, scoring conventions
- Tachykinin reference pharmacology (NKB/NKA/Substance P) as positive controls

## Approach

1. Executable cheap funnel: 3 generators -> liability filters -> motif-heuristic
   calibration -> pre-registered ranked candidate list with run records.
2. Gated docking stage: NK2R structure + active-site definition + NKB positive
   control, then HADDOCK3 candidate docking with pre-registered scoring.
3. Selectivity stage: NK1R/NK3R counter-docking.
4. Submission packaging only if the competition format is obtained and the human approves.

## Success criteria

- Funnel acceptance: calibration pass, 3 generators, none >50%, 60 ranked candidates.
- Docking acceptance: positive control validates before any candidate claim.
- Honest-negative reporting throughout; no EC50 claims without experiment.

## Open questions

- Competition portal, rules, submission format, round status (feature has none).
- NK2R structure source and binding-site definition.
- Whether an ML generator (BoltzGen) should be added as a fourth arm.
