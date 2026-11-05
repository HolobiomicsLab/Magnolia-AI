# NK2R target notes (verified facts vs unknowns)

## Verified from the Science/AAAS feature (2026-04-23 package)

- Competition: Peptide Design Competition, organized by the Beijing Frontier
  Research Center for Biological Structure (FRCBS), Tsinghua University.
- ~300 participants; 2026 winners include AngelPro Technology, ByteDance AI
  Drug Discovery, and Xiamen University researchers.
- This year's target: NK2R, a receptor involved in energy metabolism and appetite.
- Round 1: participants received NK2R structural data and generated peptide
  sequences predicted to bind and activate the receptor; submissions were
  evaluated computationally, then synthesized and tested experimentally.
  Early round-1 results: several AI-designed NK2R peptides showed measurable
  activity with EC50 values in pharmacologically relevant ranges.
- Round 2: candidates optimized on round-1 results and tested for selectivity
  against other homologous family proteins.
- Organizers hope to expand participation in future iterations.

## Standard pharmacology (reference knowledge, no structure asserted)

- NK2R = neurokinin-2 receptor, gene TACR2, class A GPCR.
- Endogenous agonist: neurokinin B (NKB), DMHDFFVGLM-NH2 (C-terminal amide).
- Family: NK1R (TACR1; substance P = RPKPQQFFGLM-NH2), NK3R (TACR3).
  Neurokinin A (NKA) = HKTDSFVGLM-NH2.
- Shared tachykinin C-terminal pharmacophore: aromatic-X-Gly-Leu-Met-NH2.

## Unknowns — must be resolved before dock_prepare

- NK2R structure: PDB ID or competition structural package. NOT yet obtained;
  no ID is asserted in this scaffold. Selection must record resolution, ligand
  state (agonist/antagonist-bound), and missing-loop handling.
- Orthosteric site definition: extracellular vestibule of the GPCR; needs a
  literature- or structure-based residue list (cf. docking-site-definition skill:
  never a subdomain label alone).
- Competition submission format, rules, and round status.
- Whether a membrane-mimetic environment is required for meaningful docking
  scores (HADDOCK3 default is aqueous; this is a real transferability risk from
  an established SBD docking setup, not a detail).
