---
confidence: 0.9
created: '2026-06-13T10:29:09.175467+00:00'
description: "Longer peptides (\u226510 residues) built with tleap can produce broken\
  \ backbone geometry (CA\u2013CA >4 \xC5) without warning. After building any input\
  \ peptide PDB, run a quick CA\u2013CA distance check: `grep ' CA ' "
id: '20260613_102909_175447'
observation_count: 2
observed_in_sessions:
- ses_0c7425453ffeDvyNU5dHhXPEh9
opencode_session_id: ses_13fc23de2ffelf2ddyTWl2olKC
source: opencode_distill
tags:
- input_validation
- peptide_geometry
- HADDOCK3_workflow
- CA_CA_distance_check
title: Verify input peptide backbone geometry before running HADDOCK3 docking
tools:
- tleap
- pdb_analysis_shell
type: workflow_note
updated: '2026-07-06T19:21:29.910069+00:00'
---

Longer peptides (≥10 residues) built with tleap can produce broken backbone geometry (CA–CA >4 Å) without warning. After building any input peptide PDB, run a quick CA–CA distance check: `grep ' CA ' peptide.pdb | awk '{print $6, $5, ...}'` and verify all consecutive distances are 3.7–3.9 Å. This catches build errors before docking. All clean inputs in this project (5–11‑mer) had no breaks; broken cases were 10‑mer QRKELx2 and 14‑mer fusion. Store rebuild scripts (e.g., `peptide_rebuild.tleap`) alongside inputs for reproducibility.

## Observation 2 (2026-07-06)

User decision: before any 10-mer extension proposal, first identify the most successful 9-mers (complete ranking), then perform thorough structural characterization of the lead candidate. The user explicitly rejected skipping to design ('make sure you have the best 9mers in the list first'; then 'do all the geometric measurement you can. make sure you understand the structure and it's surrounding environment before proposing any extension'). This workflow — rank, then structurally characterize the leader (contacts, ensemble rigidity, backbone geometry, extension directions, parent comparison), then propose grounded designs — was followed. A verification step ('check it again, make sure everything is grounded') caught multiple transcription errors, establishing a precedent that every quantitative claim must be independently re-verified against raw data before committing to design. The user's focus on grounding prevented proceeding with a flawed structural understanding.
