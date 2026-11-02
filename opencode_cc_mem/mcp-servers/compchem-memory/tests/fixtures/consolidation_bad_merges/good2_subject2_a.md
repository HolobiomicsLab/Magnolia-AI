---
confidence: 0.85
created: '2026-07-01T11:50:16.220174+02:00'
description: "In entity4 (L8\u2192K on entity6), the LYS at sequence position\
  \ 8 (PDB pos 9) contacts PHE428 (3-4 \xC5), not the intended GLU460. Contact map\
  \ of entity4 cl3 shows K9 within 3.5 \xC5 of PHE428/THR427; GLU4"
id: '20260701_115016_220174'
observation_count: 4
observed_in_sessions:
- ses_0e37fecd8ffen9fpkjK39wRRL0
- ses_0d93057c5ffejLlEWHkl51h6E5
- ses_0c7425453ffeDvyNU5dHhXPEh9
opencode_session_id: ses_0e37fecd8ffen9fpkjK39wRRL0
source: opencode_distill
tags:
- entity4
- L8->K
- PHE428
- GLU460
- designed_contact_failure
- subject3_SBDbeta
title: "entity4 L8\u2192K mutation missed designed target GLU460, hit PHE428 instead"
tools:
- contact_map
- haddock3
type: scientific_finding
updated: '2026-07-06T19:21:27.823888+00:00'
---

In entity4 (L8→K on entity6), the LYS at sequence position 8 (PDB pos 9) contacts PHE428 (3-4 Å), not the intended GLU460. Contact map of entity4 cl3 shows K9 within 3.5 Å of PHE428/THR427; GLU460 is >5 Å away. This is a failure of the design hypothesis that L8→K would target E460. CAVEAT: Only one cluster (n=8, 4%) was analyzed; alternative poses might differ, but the dominant groove-only cluster shows this miss.

## Observation 2 (2026-07-01)

In entity3 (A7→R on entity1), ARG at sequence position 7 (PDB pos 8) contacts GLU460 at 3.3 Å in the dominant cluster (n=97, 48.5% of ensemble). Alascan Δ of R7 is −12.30 (hotspot). Core footprint preserved (17/19). DockQ self-ref 1.000, AIR-free −116.51. This is the only designed basic-acidic contact that actually hit its target in the project. CAVEAT: Single self-referenced cluster; no experimental validation. The ARG mutation is stronger than LYS (compare entity2 Δ −4.05).

## Observation 3 (2026-07-03)

entity4 c3 (L8→K) is geometrically a gap-threading pose (peptide contacts both groove and lid region, confirmed by measurement: d_min_lid 6.34 Å) yet converges poorly (n=20, 10% of ensemble). This is the only cluster that is gap-threading but has low convergence among the 1e/1f data. The geometric measurement showed this cluster has the largest mean distance from the lid among gap-threading clusters (d_mean_5close 9.35 Å for entity4 c3 versus 4–5 Å for well-converged gap-threaders like entity6 c1 (4.44 Å) and entity2 c1 (4.47 Å)). This suggests that even within the gap-threading mode, tighter lid proximity (mean <6 Å) is needed for good convergence. The extra distance may be due to the K8 mutation (L8→K) not fitting well under the lid despite being in the correct geometric path.

## Observation 4 (2026-07-06)

Across all 9-mers on AF3 subject3 (s10000), the best cluster by AIR-free is entity4 clt3 at -116.52, with DockQ 0.786, irmsd 1.01 Å, bsa 1688 Å². However, this cluster has an unusually high air term (130.54 vs 28.2 for clt1), meaning it violates many p2rank-derived restraints significantly and its rank depends on preferring the AIR-free (restraint-independent) metric. The designed L8K mutation was intended to form a salt bridge to GLU460, but K8 NZ is >9 Å from every acidic residue (closest: GLU475 OE1 at 9.15 Å, GLU475 OE2 at 10.00 Å, ASP492 OD1 at 10.21 Å). Instead, K8 packs into the 426–428/470–473 groove wall (contacts: GLN426 2.3, GLY470 2.5, PHE428 2.6, GLN473 2.8 Å), suggesting the score improvement comes from improved wall packing, not electrostatic anchoring. Energy decomposition verified: vdw -62.2, elec -206.6 (0.2 weight), desolv -13.0, air 130.5 (0.1 weight), sum -103.46. Caveat: the L8K salt-bridge failure is pose-specific; another cluster of the same peptide might form it.
