# NK2R peptide-design campaign brief (v0.1, PROVISIONAL)

All choices here are provisional and subject to change. This brief follows the
campaign-protocol skill: one third science, two thirds operations.

## Science (1/3)

**Target.** NK2R (neurokinin-2 receptor, TACR2), a class A GPCR regulating
energy metabolism and appetite — this year's target of the FRCBS (Tsinghua)
Peptide Design Competition, as described in the Science/AAAS sponsored feature
"The peptide design challenge" (2026-04-23 print package). Endogenous agonist:
neurokinin B (NKB), `DMHDFFVGLM-NH2`. Selectivity family: NK1R, NK3R.

**Objective.** Produce a ranked list of peptide candidates (8–12 aa, C-terminal
amide) predicted to bind NK2R, with an auditable funnel from generation to
docking to selectivity, reusing the hsc70 peptide-docking machinery.

**Success criteria (numbers, pre-registered).**
1. Funnel instrument check: calibration passes (NKB wild-type outranks all
   decoys), ≥3 generators, none >50% of the final set, 60 ranked candidates.
2. Docking stage (when ungated): NKB positive-control docking reproduces a
   plausible NK2R binding mode before any candidate docking runs; candidates
   ranked by HADDOCK AIR-free score with the same weights as hsc70 late runs
   (w_vdw=1.0, w_elec=0.2, w_desolv=1.0, w_air=0.1) unless re-registered.
3. Selectivity stage: report NK2R score minus max(NK1R, NK3R) score per candidate.
4. Honest-negative clause: if the positive control fails, or no candidate beats
   NKB on the pre-registered score, the report says so — no cherry-picked win.

**Calibration obligations.** (a) Funnel heuristic calibrated on NKB/NKA/Substance
P vs scrambled/reversed/unrelated negatives (implemented). (b) Docking
instrument calibrated on an NKB–NK2R positive control before candidate ranking
(gated). (c) hsc70 machinery reuse is a *tooling* decision, not evidence of
transferability to a GPCR target — the membrane environment and binding site
differ fundamentally from Hsc70's SBD groove.

## Operations (2/3)

**Budgets.** Funnel stage: local, seconds, no cost. Docking stage (pre-registered
cap): ≤60 candidates × 1 HADDOCK3 run each, sampling s10000, ssh-slurm azzurra,
2 GB/core, wall-time per run as in hsc70 late runs; no optimization rounds until
round-1 results exist. No real submission spend without human approval.

**Pacing.** One stage at a time; a stage opens only when its predecessor's gate
passes. No docking before calibration of the docking instrument.

**Tool rule.** Only public-source tools, each validated on a known answer in
its first hour: HADDOCK3 (hsc70 pipeline), peptide builder (hsc70
`_peptide_build`), p2rank or literature-defined orthosteric site for the GPCR.

**Funnel order.** Composition/liability filters → motif heuristic → docking →
selectivity. Cheap before expensive, always.

**Method diversity.** Three generators (motif, mutagenesis, guided-random);
none may exceed 50% of the final set. An ML-based generator (BoltzGen or
similar) is a candidate fourth arm but is NOT required for v0.1.

**Decision records / kill ledger.** Every run writes `run-record.json`,
`kill-ledger.json`, `calibration.json`, `ranked_candidates.csv`. Rejections are
first-class artifacts.

**Supervision and stop conditions.** Human approval required before: any real
Slurm submission, any external submission to the competition, any change to the
pre-registered scoring or acceptance rules. Stop-the-line if the positive
control fails or if any acceptance item is false.

**Reporting.** Claims carry their caveat: the funnel heuristic is not a binding
model; docking scores are not EC50; no experimental activity is claimed without
wet-lab data. Failures are reported alongside successes.

## Unknowns / external dependencies (explicit)

1. **Competition rules and submission format.** The Science feature is a
   sponsored article with no rules, portal link, or timeline. Actual
   participation needs the FRCBS competition portal/organizers; this pipeline
   currently attempts the *task*, not the *submission*.
2. **NK2R structural data.** Round 1 supplied structural data to participants.
   We have not obtained it; a public NK2R structure (PDB) must be selected and
   verified before `dock_prepare` opens. No PDB ID is asserted here.
3. **Round status.** 2026 winners are already announced in the feature; whether
   a new round is open is unknown.
4. **Agonism vs binding.** The competition tests *activity* (EC50). Docking
   addresses binding only; functional agonism is out of model scope and must be
   labeled as such in any report.
