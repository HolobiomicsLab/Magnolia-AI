---
confidence: 0.65
created: '2026-06-16T16:52:52.325883+00:00'
description: All successful haddock3 runs for the subject3 substrate-binding domain (PDB
  subject1) with various peptide sequences produced exactly 2 clusters in the final caprieval
  analysis. This held across two distinct
id: '20260616_165252_325868'
observation_count: 4
observed_in_sessions:
- 2026-06-18_094647
- ses_0a6c42101ffevfoKY1bV6mQicV
- ses_fecf33ecaffeSYDQC0Tcri2MDX
source: auto_extraction
tags:
- haddock3
- caprieval
- cluster-count
- subject3
- subject1
- peptide-docking
- hotspot-anchoring
- protocol-comparison
title: Stable 2-cluster caprieval output across peptide variants and two HADDOCK3
  protocols for subject3 (subject1)
tools:
- haddock3
type: parameter_guidance
updated: '2026-08-18T04:26:59.980029+00:00'
---

All successful haddock3 runs for the subject3 substrate-binding domain (PDB subject1) with various peptide sequences produced exactly 2 clusters in the final caprieval analysis. This held across two distinct pipeline configurations:

1. 'full' protocol (e.g., subject1_entity8_s5000_full): includes rigidbody docking (01_rigidbody), seletop (06_seletop), emref (07_emref), clustfcc (10_clustfcc), and multiple caprieval steps (02, 05, 09, 12_caprieval).

2. 's5k' protocol (e.g., subject1_entity8_s5000): lacks rigidbody output; starts with flexref (3_flexref), then emref (4_emref), clustfcc (5_clustfcc), caprieval (7_caprieval), and contactmap (8_contactmap).

Peptides tested: entity8, KILDQ, entity11, QRKEL, HAMNG, fusion, and dimeric variants (x2). All yielded cluster_count=2.

CAVEAT: This observation is limited to the subject3 SBD (subject1), the specific peptide sequences listed, and the particular clustering parameters in clustfcc (default). The 2-cluster result may reflect the well-defined binding groove of Hsp70 chaperones rather than a general property. It does not necessarily generalize to other protein targets or different clustering thresholds. The actual capri_ss.tsv scores were not examined in this session.

## Observation 2 (2026-06-18)

## Finding

**w_air is NOT a constant in HADDOCK3** — it differs by pipeline stage because caprieval inherits it from the upstream CNS module:

- rigidbody (it0): w_air = 0.01
- flexref (it1): w_air = 0.1
- emref/mdref (it2): w_air = 0.1

This is the classic HADDOCK protocol design: light restraint enforcement during broad rigid-body sampling (0.01), then stronger enforcement during refinement (0.1).

## Verification

- weights_params.json from 25 completed full-protocol runs all show the same pattern: 02_caprieval=0.01, 05/09/12_caprieval=0.1
- Mathematical verification: recomputing score from raw components (vdw, elec, desolv, air) with w_air=0.1 gives Δ=0.000 for all runs
- HADDOCK3 docs confirm caprieval has no w_air parameter (it inherits from upstream CNS module)

## Impact on AIR-free computation

For final scores from 12_caprieval (post-refinement), the correct w_air is **0.1**, not 0.01. Using 0.01 under-corrects AIR by 10×. The 2026-06-17 report used 0.01 for all stages — QRKEL's AIR-free shifted from −61.38 (wrong, 0.01) to −66.54 (correct, 0.1), moving from last to 4th among 6 five-mers.

## CAVEAT

This w_air pattern (0.01 rigidbody, 0.1 refinement) is the HADDOCK3 default. If a config explicitly sets w_air in any module, always verify against that run's weights_params.json. Different HADDOCK3 versions may change defaults — re-verify after upgrades.

## Rule updated

rules/haddock3.md updated to v1.2 (2026-06-18) with stage-aware scoring table and mandatory weights_params.json check.

## Observation 3 (2026-07-14)

Submitted 4 ACE+NME capped peptide HADDOCK3 runs: Ac-TGGFQMALT-NME (9‑mer, job 11470749), Ac-entity4-NME (9‑mer, 11470750), Ac-entity5-NME (10‑mer, 11470751), Ac-NRLLLTG-NME (7‑mer, 11470752). All use the same verified parameters: s10000 sampling, 16 cores, 32 GB memory, 24 h wall time, cpucourt partition. Config files (haddock3/local) and HADDOCK3 version identical to the prior entity4 runs that completed in ~6 h. Run dirs fresh (no output/ subdirectory exists). Chain B was added to peptide PDBs at column 22. CAVEAT: s10000 sampling may deadlock under high load; existing runs showed no issue, but monitoring is advised.

## Observation 4 (2026-08-18)

Azzurra SSH key registration is admin-managed (email public key to HPC admins; no self-service portal). Two options: (a) copy the existing private key (~/.ssh/id_tjiangazr — NO passphrase) to the new machine; recommended because it avoids the admin round-trip, but requires chmod 600 and secure transfer; (b) generate new keypair and email the public key (one-time delay, cleaner separation). Copying the key does NOT weaken security because SSH 2FA (phone TOTP) is still enforced on every machine — the key alone cannot grant access. If local username differs on the new machine, cluster `User tjiang` in ssh config stays; the tunnel script's UniCA login detection may need `UNICA_USER=<login> hpc_tunnel.sh`.
