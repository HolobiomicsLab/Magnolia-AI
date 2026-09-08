# Use cases and starting prompts

Magnolia is instructed in prose. The opening message of a session therefore does more work than any
other, and this document is a catalogue of openings that have proved effective, each with the
context that makes it sensible.

## The anatomy of a good opening

A serviceable first message states four things:

| Element | Why it matters |
|---|---|
| **The objective** | Determines which skill is loaded and which instrument is proposed |
| **Where the inputs are** | Magnolia reads files; a path is worth a paragraph of description |
| **What has already been tried** | Prevents a costly repetition, and lets prior results be read rather than re-derived |
| **The criterion of success** | Without it, any output satisfies the request |

A fifth element is optional but frequently decisive: **whether you want it to act at all yet**.
Models differ markedly in eagerness, and *"discuss the approach first, do not run anything"* costs
one clause and saves an allocation.

Compare:

> *"Run the docking."*

> *"I would like to dock the peptide in `raw_input/pep_A.pdb` onto the target in
> `raw_input/target.pdb`. I ran a first attempt in June with manual restraints — see
> `runs/2026-06-14_manual/` — and I would now like to try pocket-based restraints instead, to see
> whether the interface improves. Discuss the approach before running anything."*

The second is not merely longer: it names an instrument, a comparison, a prior result to read, and a
criterion. Magnolia will typically answer with a plan and a question.

---

## 1. A structure-based campaign: pocket, dock, analyse, design

The canonical sequence. One starts from a target structure, asks where a ligand might plausibly
bind, docks a first series, reads the interaction pattern off the best poses, and lets that pattern
constrain the next round. Magnolia's contribution is not that it can call P2Rank and HADDOCK3, which
one could do by hand; it is that the pocket chosen in the first step, the restraints derived in the
second and the scores obtained in the third remain legible weeks later, when the fourth round has to
be justified to a co-author.

The campaign is naturally conducted over several sessions. Because the notebook is read at the start
of each, the second session does not begin from nothing: prior runs, the errors met and the
conventions established are already in view.

**Opening prompt**

> *"I would like to identify a binder for the target in `raw_input/target.pdb`. Please begin by
> predicting the pockets with P2Rank and telling me which one you would favour and why, before
> running anything. I have no prior results for this system. I will judge the outcome on HADDOCK
> score and on whether the interface reproduces the contacts described for the natural ligand."*

**What follows.** Magnolia runs `p2rank_predict`, reads `_predictions.csv`, and discusses the ranked
pockets in terms of score, volume and residue composition. On your agreement it derives ambiguous
interaction restraints for the chosen pocket, writes a self-contained HADDOCK3 run directory with
its `config.cfg`, verifies the inputs against
[`prejob_check.md`](../opencode_cc_mem/rules/prejob_check.md), and only then submits.

**Useful follow-ups**

> *"Analyse the binding contacts in the best-scoring cluster and tell me which positions are doing
> the work."*

> *"Given those contacts, propose restraints for a second round, and explain what you expect to
> change."*

> *"Compare this run against `runs/2026-06-14_manual/` on caprieval scores, and record the
> comparison in the notebook."*

---

## 2. Small-molecule and covalent docking

Where the ligand is a small molecule rather than a peptide, gnina replaces HADDOCK3, and the
questions change: protonation and tautomers, exhaustiveness, whether the CNN score or the empirical
score is the more trustworthy ranking for this target, and — for covalent inhibitors — whether the
warhead has been represented correctly at all. The last is the frequent silent failure: an alkyne
warhead docked in its unreacted form answers a question nobody asked.

**Opening prompt**

> *"I have a series of twelve candidate inhibitors in `raw_input/series_1/` and the target in
> `raw_input/target.pdb`. Three of them carry an alkyne warhead intended to react with Cys145.
> Prepare the covalent cases properly — I want to see the vinyl isomers you generate and the SMARTS
> you validate them against before docking. Rank on CNN affinity, but show me both scores."*

**Useful follow-ups**

> *"Which of these poses would you not trust, and why?"*

> *"Record in the notebook that the empirical score disagreed with the CNN ranking for this target,
> with the two tables."*

---

## 3. Molecular modelling in the strict sense: quantum chemistry

Geometry optimisation, vibrational frequencies, frontier orbitals, single-point energies at a better
level of theory. The chemistry is rarely the difficulty; the bookkeeping is. Which conformer, which
functional, which basis set, which revision of the programme, whether the frequency calculation was
run on the optimised geometry rather than on the input one, and whether the imaginary mode that
appeared was addressed or quietly ignored — these are the details that a methods section requires
and that memory supplies unreliably three months later.

Magnolia proposes a method and basis proportionate to the size of the system and the question,
prepares the input, submits it, parses the output for energies, HOMO–LUMO gaps and frequencies, and
records the level of theory alongside the numbers. When the SCF fails to converge it consults its
own rule for the standard remedies rather than reporting failure.

**Opening prompt**

> *"For the ligand in `raw_input/ligand_A.mol2`, I need an optimised geometry and a frequency
> calculation to confirm it is a genuine minimum, then a single-point energy at a higher level.
> Propose a method and basis set appropriate to a molecule of this size and justify the choice; use
> xTB first to pre-optimise. The charge is 0 and the multiplicity 1. Record the level of theory in
> the notebook — I will need it for the methods section."*

**Useful follow-ups**

> *"The optimisation produced one imaginary frequency. What does that indicate here, and what would
> you do about it?"*

> *"Run the same protocol on the other four conformers and give me the relative energies as a
> table."*

> *"Draft the computational-details paragraph from what is in the notebook, and flag anything you
> cannot substantiate from the record."*

---

## 4. Molecular dynamics as a validation stage

A docking pose is a hypothesis. Molecular dynamics is the usual instrument for asking whether it
survives contact with solvent and thermal motion. The stages are standardised — topology, box,
solvation, ions, minimisation, NVT, NPT, production — and each has a gate that should be inspected
before the next begins, which is precisely the discipline that is abandoned first when one is in a
hurry.

**Opening prompt**

> *"Take the top pose from `runs/2026-08-02_dock/` and set up a GROMACS simulation to check whether
> the complex is stable. Choose a force field and box appropriate to a protein–peptide complex in
> water with physiological salt, and tell me your choice before proceeding. Stop after equilibration
> and show me the energy and temperature traces; I will decide about the production run."*

**Useful follow-ups**

> *"Run 100 ns of production on the cluster, then compute backbone RMSD, radius of gyration and
> per-residue RMSF."*

> *"Does the interface identified in the docking survive the simulation? Answer from the contact
> analysis, not from the RMSD alone."*

---

## 5. Generative design

Where the previous cases refine an existing molecule, BoltzGen proposes new ones. It is driven
through the general command runner rather than a dedicated wrapper, which from your side changes
nothing except that it does not appear as a separate entry in the tool list. Design runs are long,
GPU-bound and resumable, and the resumption recipe is one of the things the corresponding skill
exists to remember.

The productive pattern is to constrain generation by what the earlier analysis established, rather
than to sample freely and filter afterwards.

**Opening prompt**

> *"Using the contact analysis we recorded for the interface — positions 1 and 4 favour a positive
> charge, position 2 an aromatic residue — set up a BoltzGen design run for a twelve-residue binder
> against the target in `raw_input/target.pdb`. Show me the design specification before submitting,
> and tell me what GPU time to expect."*

**Useful follow-ups**

> *"The run stopped at generation 3. Resume it rather than restarting."*

> *"Take the ten best designs and validate them with the same docking protocol we used for the
> natural ligand, so the scores are comparable."*

---

## 6. Literature and bibliography

With [Perspicacité](https://github.com/HolobiomicsLab/Perspicacite-AI) running on `localhost:8000`,
bibliographic work ceases to be a separate activity conducted in another window. Magnolia can
assemble a knowledge base on a question, read it, and — the part that matters — write the conclusion
into the project notebook as an annotated entry carrying its DOIs.

The consequence is worth stating explicitly, because it is the difference between a chat assistant
and a research assistant: a methodological choice and the paper that motivated it end up stored in
the same place, at the same moment. A decision taken in March can therefore still be defended in
September, and the methods section can cite what was actually read rather than what is remembered.

Two limitations should be anticipated. Paywalled full texts and supplementary material are not
retrievable through this route and require institutional access; and DOI ingestion is the step at
which malformed records tend to surface, so it is worth checking what entered the knowledge base
before reasoning over it.

**Opening prompt**

> *"Before we set up the simulation, survey the recent literature on force-field choice for
> intrinsically disordered peptides — say the last five years. Build a knowledge base, summarise
> where the field agrees and where it does not, and record the conclusion as a note in the project
> notebook with the DOIs attached. Do not start any calculation yet."*

**Useful follow-ups**

> *"Which of these papers actually measured what they claim, and which cite someone else for it?"*

> *"Add a note to the notebook that we chose CHARMM36m on the strength of reference 4, with the
> DOI."*

> *"Is there anything published since we made this decision that would change it?"*

---

## 7. A campaign on the cluster

Long calculations belong on a Slurm cluster, and the awkward part has never been submission but the
interval that follows: a job that finished on Tuesday is of no use if by Thursday nobody recalls
which configuration produced it. Magnolia submits, polls, fetches results back into the project, and
ingests the outcome into the notebook, so the record is written whether or not you were watching.

Sites are declared in configuration rather than in code; see [hpc.md](hpc.md). Personal facts — your
username, your account — belong in `~/.config/magnolia/clusters.yaml` and never in the repository.

**Opening prompt**

> *"Submit the four HADDOCK3 runs prepared in `runs/2026-09-05_series/` to the cluster. Before
> submitting, verify that the residue numbering in each restraints file matches its structure — the
> last campaign lost ninety minutes to that. Use the group account, tell me the estimated queue
> time, and poll them; I will be away this afternoon."*

**Useful follow-ups**

> *"What is the state of the four jobs, and has anything failed?"*

> *"Fetch the results of the two that completed and summarise the scores against our June baseline."*

---

## 8. Writing up

The final stage of a project is generally the least well served by tooling. Because every run
carries its date, its configuration, its outcome and its annotations, the sequence of experiments
can be reconstructed as a narrative, with what was observed kept distinct from what was inferred —
a distinction that erodes quickly in recollection and that reviewers are trained to test.

Manuscripts held on Overleaf are reachable through its git bridge, under the discipline set out in
[`overleaf.md`](../opencode_cc_mem/rules/overleaf.md): pull before push, commit granularly, verify
the merge. Skills for scientific prose, figures and mock review are available for the drafting
itself.

**Opening prompt**

> *"Summarise everything we have run on this project since June: the protocols, the parameters that
> changed between rounds, and the results. Separate clearly what was measured from what we
> concluded. Then draft a methods paragraph for each computational stage, using the parameters as
> recorded in the notebook, and flag anything you cannot substantiate from the record."*

**Useful follow-ups**

> *"Rewrite this paragraph so the claims match the evidence — do not change any number or
> citation."*

> *"Review this section as an adversarial referee would, and give me the three objections most
> likely to be raised."*

---

## 9. Stress-testing a plan before committing to it

Before an expensive decision — a design strategy, a protocol change, an interpretation that a
manuscript will rest on — an independent opinion is worth more than a confirmation. The
adversarial-review skill convenes independent model instances that carry none of Magnolia's memory
of the project, and therefore none of its accumulated commitments, and has them argue the case.

**Opening prompt**

> *"Before I commit two weeks of GPU time to this design strategy, red-team it. Convene an
> adversarial review of the plan as it stands in the notebook, and report the strongest objections
> rather than a verdict."*

---

## 10. Returning to a project after an interruption

The case that justifies the notebook. Six weeks after the last session, the question is not what the
software can do but what was already established: which protocol was adopted and why, which avenue
was abandoned and on what evidence, and which run was left unfinished.

**Opening prompt**

> *"I have been away from this project for six weeks. Summarise where we stand: what has been run,
> what we concluded, what failed and why, and what was left unfinished. Then tell me what you would
> do next, and why."*

**Useful follow-ups**

> *"Have we hit this error before?"*

> *"Are there any proposed rules waiting for review?"*

---

## Openings to avoid

| Opening | Difficulty | Better |
|---|---|---|
| *"Run the calculation."* | No system, no files, no protocol | Name the instrument, the inputs and the criterion |
| *"Why is it slow?"* | Nothing identifies what is running | *"The job I submitted twenty minutes ago seems stuck — job 4412. Would you check?"* |
| *"Fix it."* | "It" is unidentified | Quote the error, or point to the log |
| Five requests in one message | One will be missed, or executed out of order | Two or three messages, confirming each |
| Never correcting | Small deviations compound over a campaign | *"Wait — do it this way instead."* |

The general principle is the one that governs supervision of any capable assistant: state the
objective, supply the evidence, and correct early rather than thoroughly.
See [`../WORKFLOW_GUIDE.md`](../WORKFLOW_GUIDE.md) for a full worked session.
