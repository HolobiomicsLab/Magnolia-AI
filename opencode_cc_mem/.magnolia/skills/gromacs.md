---
name: gromacs
description: GROMACS molecular dynamics setup, execution, and analysis rules.
version: 1.0
last_verified: 2026-04-01
tags: [gromacs, molecular-dynamics, md-simulation]
---

# GROMACS Rules

## Overview

GROMACS is a molecular dynamics simulation package. The typical workflow involves: structure preparation, topology generation, solvation, ion addition, energy minimization, equilibration (NVT/NPT), and production MD.

## Mandatory: Self-Contained Run Directories

Every GROMACS run gets its own directory with all input files copied in:

```
gromacs_runs/YYYY-MM-DD_<name>/
├── processed.gro      # Processed structure
├── topol.top           # Topology file
├── ions.mdp            # Ion parameters
├── minim.mdp           # Energy minimization
├── nvt.mdp             # NVT equilibration
├── npt.mdp             # NPT equilibration
├── md.mdp              # Production MD
└── output/             # Generated outputs
```

## Essential Commands

```bash
gmx pdb2gmx -f input.pdb -o processed.gro -p topol.top -ff amber99sb-ildn -water tip3p
gmx editconf -f processed.gro -o boxed.gro -c -d 1.0 -bt dodecahedron
gmx solvate -cp boxed.gro -p topol.top -o solvated.gro
gmx grompp -f ions.mdp -c solvated.gro -p topol.top -o ions.tpr
gmx genion -s ions.tpr -p topol.top -o ionized.gro -pname NA -nname CL -neutral
gmx grompp -f minim.mdp -c ionized.gro -p topol.top -o em.tpr
gmx mdrun -s em.tpr -deffnm em
gmx mdrun -s md.tpr -deffnm md -nt 4
```

## Force Field Selection

| System | Force Field | Water Model |
|--------|------------|-------------|
| Protein-ligand | amber99sb-ildn | tip3p |
| Protein-protein | amber99sb-ildn | tip3p |
| Membrane | charmm36 | tip3p |
| DNA/RNA | amber99sb-ildn | tip3p |

## Box Types

- `dodecahedron`: Most efficient for globular proteins (recommended)
- `cubic`: Simple, wastes some solvent
- `octahedral`: Good for roughly spherical proteins
- `triclinic`: Most general, rarely needed

## Critical Parameters

- Box distance (`-d`): minimum 1.0 nm to avoid periodic image interactions
- PME cutoffs: 1.0 nm for both Coulomb and van der Waals
- Time step: 2 fs with LINCS constraints on bonds to hydrogen
- Temperature: 300 K (physiological) or 310 K (body temperature)

## Common Mistakes

| Mistake | Correct |
|---------|---------|
| Using `truncated octahedron` | Use `dodecahedron` (same geometry, better name) |
| Forgetting `-maxwarn 2` in grompp | Add when appropriate, but check warnings first |
| Not neutralizing the system | Always add counterions with `-neutral` |
| Missing position restraints | Include `posre.itp` in topology |
| Running mdrun without minimization | Always minimize first |
| Wrong group selection in genion | Select "SOL" group (usually 13 or 15) |

## Energy Minimization

- Use steepest descent integrator
- Maximum 50000 steps
- Convergence: emtol < 1000 kJ/mol/nm
- Check that maximum force decreases

## Equilibration Protocol

1. NVT (100 ps): stabilize temperature, position restraints on protein
2. NPT (100 ps): stabilize pressure, position restraints on protein
3. Production: no position restraints

## Analysis Commands

```bash
gmx energy -f md.edr -o energy.xvg          # Extract energy terms
gmx rms -s md.tpr -f md.xtc -o rmsd.xvg     # RMSD over time
gmx gyrate -f md.xtc -s md.tpr -o rg.xvg    # Radius of gyration
gmx rmsf -f md.xtc -s md.tpr -o rmsf.xvg    # Per-residue fluctuation
```

## Stage Gates

- Check energy minimization converged before equilibration
- Verify temperature stabilized during NVT (< 5 K drift)
- Verify pressure stabilized during NPT (< 50 bar drift)
- Production MD: at least 50 ns for binding free energy calculations
