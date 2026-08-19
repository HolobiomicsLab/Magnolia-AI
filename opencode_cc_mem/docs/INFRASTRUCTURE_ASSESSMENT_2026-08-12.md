# Self-Assessment: Ad Verum Infrastructure vs. LLM Native Capability in the GPX4 Campaign

**Date:** 2026-08-12
**Scope:** The 2026-08-10 → 2026-08-12 GPX4 covalent-inhibitor design campaign
(Quino batch → regioisomer screen → design rounds 1–5 → phases 2–3 → v5 series),
which progressed the best docking score from −4.93 to −11.8 kcal/mol.
**Status:** Author's (agent's) self-assessment requested by the user. Deliberately
NOT recorded as a project learning — this is a meta-evaluation of the tooling,
not a scientific finding.

---

## 1. What the infrastructure demonstrably contributed

### 1.1 Non-repetition of expensive mistakes (highest value)

The frozen docking protocol used all campaign — `--cnn_scoring none`, inspect ALL
5 modes, dock-the-product-not-the-warhead, Z-isomer only, seed 42 for
comparability — came from July memory entries, not from in-session reasoning.
Without them, the known failure modes (CNN mis-ranking of covalent poses, the
v3_2 Mode-1 trap with its 2.17 kcal/mol gap, warhead-vs-product docking) would
likely have recurred. Weeks of prior debugging were available in seconds.

### 1.2 Cross-project transfer

The contact-analysis discipline ("never claim an interaction mechanism from
residue-type labels; measure atom-level distances") was a skill-tier entry from
the June Hsc70 project. It directly shaped the GPX4 contact analysis that
produced the LEU44/GLN45 correction — a different project, six weeks later.
This is the memory architecture working exactly as designed.

### 1.3 Comparability and traceability

The `runs/YYYY-MM-DD_name/` conventions, `submit_job` lifecycle tracking, and
per-round `analogs.csv` + `round_summary.md` structure are what make the
−4.93 → −11.8 evolution reconstructable by a third party. The audit-before-trust
rule (now embedded in GOAL.md) caught real artifacts (cis→trans vinyl escapes).

## 2. Where the infrastructure failed or was neutral

- **Data misplacement (active failure):** the demo-vs-gpx4 project-pinning bug
  (`start-adverum.sh` reuse branch skipping the serve restart) sent a session's
  learnings to the wrong project and cost debugging time. Root-caused in-session;
  the fix pattern is documented but the script itself is unchanged.
- **Auto-distillation noise:** duplicate staging entries required manual merge
  twice; one auto-extracted entry overstated the regiochemistry claim ("forced
  by SMARTS") and needed user correction.
- **Tooling gaps:** `gnina_parse_results` does not handle the multi-model PDB
  outputs this pipeline produces; the gnina raw binary fails under `submit_job`
  without `LD_LIBRARY_PATH` (one lost batch attempt).
- **Zero design ideas.** No memory entry, rule, or tool proposed naphthyl, the
  phenethyl second arm, the TYR96 import, or the α/β screen.

## 3. What came from native LLM capability

Essentially all hypothesis generation and scientific judgment:

- Per-round design logic (which analog, aimed at which contact, and why).
- The PDB-bond-order hypothesis for the trans escapes — wrong, but cheaply and
  definitively falsified by the SDF A/B re-dock.
- The two-arm hybrid concept (pose comparison → unoccupied volume → arm aimed
  from the amide nitrogen), which produced the −9.45 compound in one shot.
- Mid-session tool-building: `audit_poses.py`, `contact_analysis.py`,
  `accessible_targets.py`.
- Failure diagnosis (duplicate atom names, one-way CONECT records, cudnn env).

Also most of the errors: the ASN137 phase-2 target was an LLM mistake
(distance-only reasoning), caught by the user ("137 is buried under 136") and
converted into the occlusion-aware accessibility tool.

## 4. The third factor: user steering

A fair accounting must include the user's interventions, which were few but
pivotal:

- "Does Se always bind to the same C?" → triggered the α/β regioisomer screen,
  the single highest-leverage experiment of the campaign.
- "The 44 idea was from v4_5, why do you say 47–44 are not touched?" → forced a
  correction of an over-claim.
- "137 is not accessible" → killed a doomed design round before it ran.
- The Phase-1/Phase-2 framing → structured the final design iterations that
  produced the −11.8 compounds.

## 5. Bottom line

Rough judgment, not measurement:

| Contributor | Share | Nature of contribution |
|---|---|---|
| Infrastructure (memory, rules, tools) | ~25% | Continuity, discipline, comparability, mistake-prevention |
| LLM native reasoning | ~55% | All design, interpretation, tool-building, falsification — and most errors |
| User steering | ~20% | Few interventions, disproportionately high leverage |

The infrastructure and the LLM are multiplicative, not additive: memory
amplified reasoning (the loaded protocol freed the session for design work), and
reasoning amplified memory (each session's entries are richer than the last).
But the novel science — the final compounds and the design rules — was reasoning
over data, with user corrections at exactly two critical moments. The
infrastructure's main verifiable claim is narrower and still worth having:
**nothing had to be discovered twice.**

## 6. Improvement suggestions (for the infrastructure roadmap)

1. Fix the project-pinning failure mode in `start-adverum.sh` (bounce the serve
   or refuse when the rendered project differs from the running one).
2. Dedupe auto-distilled staging entries against existing ones before writing.
3. Extend `gnina_parse_results` to multi-model PDB outputs (or emit SDF).
4. Bake the `LD_LIBRARY_PATH` requirement into the submit_job environment for
   gnina instead of relying on each script to remember it.
5. Consider promoting `audit_poses.py`-style geometry gating into the
   compchem-tools stage-gate registry so it runs automatically after docking.
