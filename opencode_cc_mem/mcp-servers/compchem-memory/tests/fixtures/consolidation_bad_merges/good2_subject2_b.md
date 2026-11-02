---
confidence: 0.7
created: '2026-07-01T11:50:16.991367+02:00'
description: "Alascan on entity4 shows G2 (sequence position 2, first glycine) is\
  \ tolerant (\u0394 \u22120.08) with 16% void and GLU460 at 3.2 \xC5. G2\u2192K would\
  \ add a K2\u2192E460 contact alongside the existing K8 (which missed E46"
id: '20260701_115016_991367'
observation_count: 3
observed_in_sessions:
- ses_0e37fecd8ffen9fpkjK39wRRL0
opencode_session_id: ses_0e37fecd8ffen9fpkjK39wRRL0
source: opencode_distill
tags:
- EGKFQMAKT
- G2->K
- double_mutant
- GLU460
- subject3_SBDbeta
- entity4
title: "EGKFQMAKT double mutant (G2\u2192K on entity4) proposed as strongest next\
  \ candidate"
tools:
- alascan
- void_analysis
- contact_map
type: scientific_finding
updated: '2026-07-01T15:16:30.072861+00:00'
---

Alascan on entity4 shows G2 (sequence position 2, first glycine) is tolerant (Δ −0.08) with 16% void and GLU460 at 3.2 Å. G2→K would add a K2→E460 contact alongside the existing K8 (which missed E460). This double mutant (EGKFQMAKT = E-G-K-F-Q-M-A-K-T) combines the best scoring rare pose (−118.59) with a second designed anchor. CAVEAT: Parent entity4 is only 4% groove-only; the double mutant may further destabilize the binding mode. Void analysis may overpredict (see dedicated entry).

## Observation 2 (2026-07-01)

In entity4 (sequence EGG...), the first glycine (G2, PDB residue 3 with ACE cap) shows Δ−0.08 (tolerant), 16% void, and is within 3.2 Å of GLU460. This makes it a stronger candidate for mutation than the second glycine (Δ+0.44, 5% void, 4.1 Å). The proposed G2→K mutation introduces a second anchor (K2) alongside the existing K8 to stabilize the 4% minority cluster. Determined via alascan and contact map analysis. Caveat: only tested computationally; binding requires experimental validation.

## Observation 3 (2026-07-01)

Mutating G2→K in entity6 (producing EKGFQMALT) and docking via HADDOCK3 gives best groove-only cluster (DockQ 1.000) AIR-free −109.56 (n=9, 4.5%) vs parent entity6 cl1 AIR-free −110.86 (n=136, 68%). Score is essentially unchanged (−1.3), but convergence drops from 68% to 4.5%. The majority of models (cl2, 41%; cl3, 31%) adopt diffuse alternative poses with poor DockQ. This suggests the introduced lysine at position 2 does not stabilize the designed E460 contact in the groove-only mode; instead, the charged residue disrupts the groove binding ensemble, pushing the system into alternative binding modes. The groove-only pose quality is preserved but becomes a rare event.
