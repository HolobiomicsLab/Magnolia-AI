---
confidence: 0.85
created: '2026-06-03T14:54:26.638846+00:00'
description: "Symptoms: Contact analysis using `clusterN_heavyatoms_interchain_contacts.tsv`\
  \ gave incomplete residue counts. Hydrogen-bond-based contacts (e.g. sidechain NH2\xB7\
  \xB7\xB7O=C) were systematically missing.\nCaus"
id: '20260603_145426_638818'
observation_count: 1
source: auto_extraction
tags:
- haddock3
- contact-analysis
- heavyatoms
- hydrogen
- interaction-typing
- caprieval
title: "haddock3 contact analysis: heavyatom files exclude hydrogen \u2013 always\
  \ measure from PDB or allatom files"
tools:
- haddock3
type: error_resolution
updated: '2026-06-03T14:54:26.638852+00:00'
---

Symptoms: Contact analysis using `clusterN_heavyatoms_interchain_contacts.tsv` gave incomplete residue counts. Hydrogen-bond-based contacts (e.g. sidechain NH2···O=C) were systematically missing.
Cause: The `*heavyatoms*` file in haddock3 output strips all hydrogen atoms before computing contacts. Contacts mediated by hydrogen (e.g. most classical H-bonds) are invisible in this file.
Fix: Use the `clusterN_allatom_interchain_contacts.tsv` file instead. When that is unavailable, fall back to measuring contacts directly from the PDB files in `structures/it1/` or `structures/it0/`.
Red herrings eliminated: The missing contacts were NOT due to wrong residue selection or misconfigured caprieval parameters.
