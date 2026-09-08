# Tool reference

Magnolia's capabilities reach the model as tools over the
[Model Context Protocol](https://modelcontextprotocol.io). Two servers are shipped in this
repository — **compchem-tools** (36 tools) for execution, and **compchem-memory** (24 tools) for the
notebook — and a third, [Perspicacité](https://github.com/HolobiomicsLab/Perspicacite-AI), is
optional and external.

You do not call these by name. They are listed here because knowing what exists makes it easier to
ask for it, and because an absent tool is the usual explanation when Magnolia declines to do
something it did yesterday: see [getting-started.md § 6](getting-started.md#6-when-something-does-not-work).

---

## compchem-tools

### Docking — protein, peptide, protein–protein

| Tool | Function |
|---|---|
| `haddock3_run` | Validate inputs, write the configuration if needed, launch HADDOCK3 in the run directory |
| `haddock3_parse_results` | Parse `caprieval` / `clustfcc` / `seletopclusts` output |
| `generate_restraints` | Derive ambiguous interaction restraints from active/passive residue files |

### Docking — small molecule and covalent

| Tool | Function |
|---|---|
| `gnina_dock` | Run gnina docking, classical or covalent |
| `gnina_parse_results` | Parse the output SDF for scores and pose information |
| `smarts_validate` | Validate a SMARTS pattern, optionally against a SMILES molecule |
| `alkyne_to_vinyl` | Generate the Z and E vinyl isomers of an alkyne warhead for covalent docking |

### Binding sites

| Tool | Function |
|---|---|
| `p2rank_predict` | Predict and rank binding pockets on a protein structure |

### Molecular dynamics

| Tool | Function |
|---|---|
| `gromacs_setup` | Topology, box, solvation and ions |
| `gromacs_run` | Run a simulation from a `.tpr` file |
| `gromacs_parse` | Extract energy terms from `.edr` and trajectory information |

### Quantum chemistry

| Tool | Function |
|---|---|
| `orca_setup` / `orca_run` / `orca_parse` | ORCA input generation, execution, and parsing of energies, HOMO–LUMO gap and converged geometry |
| `gaussian_setup` / `gaussian_run` / `gaussian_parse` | The same for Gaussian, including frequencies |
| `xtb_optimize` | GFN2-xTB geometry optimisation |
| `xtb_singlepoint` | GFN2-xTB single-point energy |

### Structure preparation and verification

| Tool | Function |
|---|---|
| `preprocess_pdb` | Add chain identifiers, correct atom names, remove waters |
| `validate_structure` | Sanity checks on a PDB or SDF: atom count, chains present, non-degenerate coordinates |
| `run_acpype` | Ligand parameterisation, with atom-type post-processing |
| `check_environment` | Confirm a binary is available, report its version, check the environment |
| `stage_gate` | Run a named gate check before proceeding to the next stage |

### Execution and jobs

| Tool | Function |
|---|---|
| `submit_job` | Submit to Slurm, PBS, SSH-Slurm, or run locally |
| `check_job` / `cancel_job` | Status and cancellation |
| `poll_jobs` | One sweep of the asynchronous life-cycle over all tracked jobs |
| `fetch_job_results` | Bring a remote run directory back to its recorded local path |
| `check_run_status` | Whether a computation has completed |
| `run_progress` | Progress of a long-running job: module completion, elapsed time |
| `list_sessions` | Active computation sessions and their status |
| `run_shell` | Run a command through `magnolia-run`, which journals it |

### Workflow and display

| Tool | Function |
|---|---|
| `workflow_load` | Load and validate a YAML workflow template |
| `workflow_status` | Which steps are complete, judged from output files |
| `show_structure` | Display a structure in the workbench viewer; performs no computation |

**Generative design.** BoltzGen is driven through `run_shell` rather than a dedicated wrapper, and
therefore does not appear as an entry above. The protocol, including the resumption recipe and the
expected GPU time, is in the `boltzgen` skill.

---

## compchem-memory

### Retrieval

| Tool | Function |
|---|---|
| `memory_get_context` | Assemble the context relevant to a stated task, re-ranked; Magnolia's first action |
| `memory_search` | Keyword and tag search across project, staging and session tiers |
| `memory_search_errors` | Find past errors resembling the present one, with their resolutions |
| `memory_select_relevant` | Semantic selection among project-tier entries |
| `memory_scan_headers` | Fast scan of entry frontmatter, without full content |
| `memory_get_run_history` | The project's run records |
| `memory_get_goal` | The current project objective |

### Capture

| Tool | Function |
|---|---|
| `memory_record_session` | Append a structured entry to the session journal |
| `memory_record_run` | Append a run record to the project index |
| `memory_record_learning` | Propose a project-tier entry with typed frontmatter, into staging |
| `memory_annotate` | Write a human-authored notebook entry |
| `memory_set_goal` | Set the persistent objective against which work is judged |
| `post_run_assess` | After a computation: exit code, expected outputs, verdict |

### Consolidation and promotion

| Tool | Function |
|---|---|
| `memory_extract_from_session` | Distil session logs into typed staging entries |
| `memory_distill_session` | Distil the current session into project-tier learnings |
| `memory_compact_session` | Prune old tool results, generate summary notes |
| `memory_consolidate` | Merge duplicates, expire stale entries, keep within budget |
| `memory_confirm` | Confirm a staging entry into the active project entries |
| `memory_review_consolidation` / `memory_apply_consolidation` | Render, then apply, pending consolidation proposals |
| `memory_review_promotions` / `memory_apply_promotions` | Render, then apply, pending elevations of a note into a rule |

### Inspection

| Tool | Function |
|---|---|
| `memory_notebook` | A chronological laboratory notebook assembled from sessions, runs and entries |
| `memory_health_check` | Audit for staleness, contradictions, gaps and orphaned entries |

Nothing in the promotion pair writes without your decision; see
[memory.md § From a note to a rule](memory.md#from-a-note-to-a-rule).

---

## Command-line interface

Some of the same functions are reachable without a session, which matters for results produced
outside one — a job that finished overnight, a command you ran yourself:

```bash
magnolia-memory log-bash    …   # record a command and its outcome
magnolia-memory log-event   …   # record an event
magnolia-memory sync-queue  …   # ingest queued events into the notebook
magnolia-memory init-vault --project-dir opencode_cc_mem/projects/my_project
magnolia-memory generate-daily-note --project-dir opencode_cc_mem/projects/my_project
```

And the launchers:

| Command | Function |
|---|---|
| `magnolia setup <project>` | Configure and verify the main and memory models |
| `magnolia <project>` | Start a session; also starts the tools daemon |
| `magnolia --critic <project>` | The same, with the flag-only claim critic enabled |
| `magnolia memory set <model> [--provider P]` / `magnolia memory status` | Manage the memory model |
| `magnolia-run <command…>` | Run a command and journal it into the session log |
| `magnolia-selfreflex <project-dir>` | Unattended consolidation; intended for cron |
| `compchem-tools-daemon.sh start\|stop\|status\|restart` | Supervise the tools daemon; ordinarily automatic |
