---
author: auto
confidence: 0.9
created: '2026-06-02T12:40:49.333419+00:00'
date: '2026-06-02'
description: '- **Symptoms:** Claimed pi-stacking between F648 (KFERQ) and A-15-TYR
  based on both being aromatic residues in the same binding pocket, using only HADDOCK3
  contact-type labels (e.g., "apolar-apolar", '
id: '20260602_124049_333361'
last_verified: '2026-06-02'
observation_count: 2
observed_in_sessions:
- 2026-06-02_064401
- 2026-06-03_084334
source: auto
tags:
- contact-analysis
- pi-stacking
- verification
- distance-measurement
title: "Contact analysis: residue-type labels are not interaction mechanisms \u2014\
  \ always measure distances"
tools: []
type: error_resolution
updated: '2026-06-03T09:24:42.741825+00:00'
---

- **Symptoms:** Claimed pi-stacking between F648 (KFERQ) and A-15-TYR based on both being aromatic residues in the same binding pocket, using only HADDOCK3 contact-type labels (e.g., "apolar-apolar", "polar-polar").
- **Cause:** The contact map's `contact-type` column classifies residue pairs by physico-chemical category (polar, apolar, positive, negative), not by actual interaction mechanism. Two aromatics in proximity does not imply pi-stacking.
- **Fix:** Measured ring centroid distances directly from the PDB. TYR15-F648 centroid distance = 10.0 Å — far outside pi-stacking range (<5.5 Å). The actual contacts: TYR15 cradles peptide C-terminus (E649-R650-Q651 at 3.0-3.9 Å), while F648 buries into the glycine-rich pocket floor (A-272, A-339, A-342, A-366 at 3.1-4.6 Å).
- **Rule:** Never claim a specific interaction mechanism (pi-stacking, H-bond, salt bridge) from contact-type labels alone. Measure atom-level distances. Contact-type labels are for coarse classification; they say "these residue types are close" not "this is the interaction."
- **Also:** This is why "grounding claims in data" in magnolia.md requires citing specific artifacts — the contact-type label was the artifact I had, and I over-interpreted it.

## Observation 2 (2026-06-03)

- **Symptoms:** Contact analysis based on `clusterN_heavyatoms_interchain_contacts.tsv` showed only loose backbone-to-backbone contacts (≥3.6 Å). Key salt bridges and H-bonds were invisible because the file name says "heavyatoms" but it **excludes hydrogen atoms** from the distance calculation. Claimed no salt bridges between K1-D366 and E3-R272 based on 3.8–4.5 Å heavy-atom distances.

- **Cause:** The `heavyatoms_interchain_contacts.tsv` file reports distances between non-hydrogen atoms only. The `interchain_contacts.tsv` file's `shortest-dist` column includes H atoms and reports much shorter distances (e.g., 2.7 Å for D366-K1 vs 3.8 Å in the heavyatoms file).

- **Fix:** Always measure directly from the PDB model (with hydrogens) to determine interaction mechanisms. The PDB models in `11_seletopclusts/` include polar hydrogens. For the KFERQ cluster 1 model 1:
  - K1(LYS)-D366: 1.6 Å OD1↔HZ2 — confirmed salt bridge
  - E3(GLU)-R272: 1.8 Å HH11↔OE2 — confirmed salt bridge  
  - E3(GLU)-T37: 1.8 Å HG1↔OE1 — H-bond
  - F2(PHE)-R272: 2.2 Å HH12↔backbone O — backbone H-bond (not cation-π as the `positive-apolar` contact-type might suggest)

- **Also:** Contact-type labels (`positive-apolar`, `negative-positive`) are residue-category classifications, not interaction mechanisms. R272-F648 is labeled `positive-apolar` because Arg=positive and Phe=apolar, but the actual shortest interaction is Arg guanidinium NH → Phe backbone O (a polar H-bond). Never infer mechanism from contact-type alone.

- **Procedure:** For each residue pair of interest, extract the actual atom-level shortest distance from the PDB. For HADDOCK3: use the models in `output/11_seletopclusts/cluster_N_model_M.pdb.gz` (gunzip first). Measure with biopython or a simple distance script. Salt bridge: oppositely charged groups ≤4.0 Å (N–O). H-bond: donor–acceptor ≤3.5 Å. Cation-π: cation center to ring centroid ≤6.0 Å. π-stacking: ring centroid ≤5.5 Å, angle <30°.
