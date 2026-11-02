---
name: docking-site-definition
description: "Choosing the docking active site and receptor conformational state before docking: use experimental ligand contacts or a validated pocket predictor, never a subdomain label alone; match the state to the biological question. Use when setting up a docking run with HADDOCK3, Gnina, or any other docking tool, or when deciding which pocket/state to target. Do NOT use for running the docking job itself or for scoring."
metadata:
  version: "1.0"
  last_verified: "2026-10-23"
  tags: "[docking, active-site, pocket-selection, receptor-state, p2rank]"
---

# Define the docking site before docking

## Rule

Do not choose the active site from a subdomain name alone. Use:

1. experimentally observed ligand-contact residues from a relevant complex, or
2. a validated pocket predictor for the system.

A subdomain label is a hypothesis, not evidence.

## Choose the receptor state

Use the conformational state that matches the biological question and the
ligand-binding mode. If the site is formed or exposed only in one state, use
that state. Do not default to open or closed by habit.

## Procedure

1. State the biological question and the expected binding mode.
2. Choose a structure or model of the relevant state.
3. Extract tight ligand-receptor contacts from a complex, or cross-check a
   validated pocket predictor.
4. Use those residues or that pocket as the active site.
5. Verify convergence. Positive scores with a very large restraint penalty
   (e.g., AIR in HADDOCK3) indicate a failed run; inspect the binding mode.

## Caveats

- A poor result can come from the restraint scheme, not only from the site
  choice.
- A pocket predictor must be validated for the system; the top-ranked pocket
  is not automatically the correct site.
- The state choice depends on the question: open-state processes need the
  open state.
