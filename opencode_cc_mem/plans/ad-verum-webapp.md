# Ad Verum — Unified Science Workbench (concept)

**Status:** concept stage. "Ad Verum" is a *placeholder* codename (Latin, "toward
the truth"; chosen to sit in the same register as French big-science names like
*Adastra* = *ad astra*). Not started; PoC design only.

**Origin.** Emerged from a strategic discussion following Anthropic's *Claude
Science* release (2026-06-30), CAS's *ScienceOne* (磐石, 2025), OpenAI's
*GPT-Rosalind*, and Google's *Gemini for Science* — and CNRS Chemistry's stated
interest in "something similar." The conclusion of that discussion: CNRS does
not need to procure or clone a foreign product. The Holobiomics lab (+ Inria
Wimmics collaborators) already owns most of the components. The gap is a
unified, low-friction surface and a sovereign backend. This doc captures that
idea.

---

## 1. The problem

CNRS chemists need an AI workbench for daily science (literature, docking, MD,
parameterization, spectra). Today:

- **Foreign products** (Claude Science, GPT-Rosalind, Gemini for Science) are
  polished but (a) route data through US model providers, (b) cannot legally
  reach CNRS-licensed paywalled literature, (c) are not sovereign.
- **The group's own components** are capable but fragmented across repos with
  no unified surface and high setup friction (MCP ports, `uv sync`, multiple
  API keys) — unusable by a non-computational chemist.

## 2. Positioning (the strategic frame)

Be **CNRS Chemistry's internal AI-for-science toolbuilder**, not a commercial
competitor to Claude Science. The directorate has (per the team) identified the
group as the *sole* team doing this line of work and is supportive even of
"just building tools for other researchers." That reframes the bar from
"out-polish Anthropic" to **"deliver reliable, sovereign, grounded tools to
CNRS chemists"** — which the group is structurally positioned to do.

### The moat (structural, not skill-based)
Two CNRS assets no foreign competitor can replicate *within CNRS*:
- **Jean Zay** (IDRIS, CNRS HPC) → sovereign model inference.
- **CNRS-licensed literature** → sovereign data grounding (Perspicacité already
  has the cookie-replay plumbing; see §6).

Plus the one capability no competitor has at all: **Magnolia's cross-session
institutional memory** (3-tier learning loop with promotion gates).

## 3. The four user modes (one surface, no seams the chemist sees)

| Mode | Chemist does | Backend(s) |
|---|---|---|
| **Ask** | "What's known about target X / this spectrum?" | Perspicacité RAG + CNRS-licensed literature |
| **Run** | "Dock this ligand" / "parameterize this peptide" | Magnolia agent → compchem-tools → cluster |
| **Explore** | "Try several protocols / reproduce this paper" | Mimosa, on-demand (the expensive path) |
| **Remember** | "What did my lab learn? why these params?" | Magnolia memory tiers |

PoC scope is **Ask + Run** only (Explore/Remember are later phases).

## 4. Architecture (layered)

```
Browser :3000        Next.js web app (extends Perspicacité)
    │  SSE
FastAPI :8000        Perspicacité backend + new "agent bridge" endpoints
    │  HTTP
opencode serve       ONE instance, the web-app's backend process
    │  hosts many sessions
Magnolia session     model + compchem-tools MCP + compchem-memory MCP + AGENTS rules
                     (this is "Magnolia running")
    │
Azzurra (PoC) / Jean Zay (production)   ssh-slurm compute
Mistral + Halo domain models            sovereign inference (production)
```

**Why this shape.** Magnolia today runs as an opencode agent session in a
terminal. A browser cannot drive that directly, so `opencode serve` mediates:
it is a headless HTTP server hosting the agent sessions, and the web app's
FastAPI proxies browser requests to it. This is **proven, not hypothetical** —
the project memory's CAPSTONE entry (`20260625_115639_901242`) demonstrated
`POST /session/{id}/prompt_async` → `GET /session/{id}/message`, headless MCP
tool-use, and a multi-agent A→B→A loop.

**The constraint (already in memory).** The serve must be the *single* opencode
instance for its sessions. Spawning separate opencode processes contends on the
shared SQLite DB and hangs at init — recorded in staging
`20260701_112742_290097`. Rule: *one serve, many sessions; isolate the data
dir so it doesn't fight the user's interactive session.*

**Deployment model.** Hosted on CNRS infra (browser access), not a desktop
install. Double win: zero install for the chemist (kills the `uv sync`/port/key
friction) and sovereign by construction (data never leaves the CNRS perimeter).
This is a structural advantage over Claude Science's desktop model.

## 5. Component roles (how existing projects layer)

| Component | Role in Ad Verum | Status |
|---|---|---|
| **Perspicacité** | web frontend (Next.js, CNRS identity), Ask mode (RAG), the async-job/SSE pattern reused for Run mode | ~80% of the unified frontend already exists |
| **Magnolia** | the agent spine (runs in opencode serve sessions) + the memory layer (compchem-memory MCP) + operational discipline (prejob checks, grounding rules, run lifecycle) | operational today (single-user) |
| **compchem-tools** | HPC execution (gnina, haddock3, gromacs, xtb, orca, gaussian, p2rank) over ssh-slurm | operational; amber wrapper = net-new |
| **Mimosa** | Explore mode — Darwinian workflow evolution, on-demand callable behind the surface | research-stage; integrate later |
| **Toolomics** | MCP tool marketplace for Mimosa | exists |
| **SciLEx** (Inria Wimmics) | literature exploration under Perspicacité | exists |

## 6. Sovereignty stack (the structural moat, concretely)

Two ideas that convert the group's weaknesses into structural advantages:

1. **Jean Zay as inference backbone.** Sovereign compute. Honest caveat: GLM is
   Chinese, so "GLM on Jean Zay" = sovereign compute + foreign brain. Clean
   *production* story = **Mistral (general, EU) + domain models** — **deferred:
   not in the near-term PoC**; kept here as the production sovereign-compute
   target, not a current step. Note: a sovereign-domain
   model thread already exists in the lab — `perspicacite_v2/perspicacite_design_1.jsx`
   references *Halo-1*, an in-house 220M-param model trained on natural-product
   corpora at ICN UMR 7272.
2. **Perspicacité ↔ CNRS-licensed literature.** Sovereign data. The plumbing
   already exists and is matured: `perspicacite import-browser-cookies` CLI,
   `cookies_path`/`cookie_domains` config, a dated QA protocol
   (`MANUAL_QA.md`, 2026-05-14). What's missing: a clean CNRS SSO path (vs
   manual cookie export) and **legal clearance** — the EU TDM Directive
   (2019/790) gives research orgs text-mining rights on lawfully-accessed
   content unless reserved; needs CNRS IP counsel sign-off, not my assertion.

These two compose to fix the panel's hardest finding (sovereignty) at *both*
layers — compute and data — which is the first time the "sovereign" claim has a
structural basis rather than a config swap.

## 7. Where Magnolia's memory lives (the crown jewel)

Magnolia's 3-tier memory (staging → project → skill, with provenance-aware
retrieval and promotion gates) **stays as the `compchem-memory` MCP server**.
opencode serve's agents call it as a tool. It does *not* move or merge into
anything else. If `oh-my-opencode` (OMO) is adopted as the orchestration
plugin, OMO is orthogonal — it changes which agents live inside the serve
(Sisyphus, Team Mode), not the topology; the browser→FastAPI→serve→sessions
layering is unchanged. The discipline: OMO's session/team memory handles
within-run state; Magnolia's memory is the *only* one that does cross-run
institutional learning. Keep them in their lanes.

## 8. PoC plan (local, Azzurra-backed)

**Scope:** Ask (existing Perspicacité RAG) + Run, with three tools:
**haddock3**, **amber**, **gnina**. Local deployment. Azzurra as compute
backend (Jean Zay deferred to production).

**Azzurra inventory (verified 2026-07-01):**
| Tool | Status |
|---|---|
| haddock3 | `haddock3/local` (user module) — present |
| amber | `amber/24` system module (full, licensed: cuda-mpi/cuda/mpi/serial) — present; `tleap`/`sander`/`pmemd` after `module load` |
| gromacs | 2024.1 + 2025.1 — present |
| xtb | `xtb/local` — present |
| **gnina** | **NOT present** — needs user-local install (~/software/gnina + ~/modulefiles/gnina/local.lua) |
| account | `spectrometry`/`qos_spectrometry` (working); `users`/`suspended` is the bad default |

**PoC phasing:**
- **PoC-1 = haddock3** (Run tab: receptor + peptide → `tleap`/amber24 build + parameterize → `haddock3` dock → caprieval scores → 3D pose render). Uses only what's installed; the tleap→haddock3 handoff shows tools composing in one surface from day one.
- **In parallel:** install gnina user-local on Azzurra; smoke-test a CNN score on `gpu`.
- **PoC-2 = gnina** Run tab.
- **PoC-3 = amber/tleap** wired as the peptide-build step feeding haddock3.
- **PoC-4 = the visible "why" line** (reads Magnolia memory_get_context; shows "ran with these params because of lesson X from run Y").

**Net-new work for PoC:**
1. Thin **Magnolia HTTP gateway** in Perspicáte's FastAPI (proxy to opencode serve; reuse the async-job/SSE pattern).
2. **Run tab + native renderers** (3Dmol.js for poses, RDKit/Kekulé.js for 2D ligands).
3. **amber wrapper** in compchem-tools (tleap/sander; AmberTools-free path is the open alternative if license is a concern).
4. The visible parameter-grounding line.

## 9. Multi-user model

The user population is **mostly computational chemists** — dozens of technical
users, not thousands, whose heavy work (docking, MD, parameterization) is
submitted to a scheduler and takes minutes-to-hours. That sizing drives the
whole design: this is *not* a high-concurrency interactive backend, it is a
thin non-blocking session router in front of an async job system.

**Concurrency lives in slurm, not the serve.** The serve never runs the
expensive work — it *submits and polls*. Real multi-user concurrency is
handled by slurm on Azzurra/Jean Zay (a proven multi-user queue, with
`qos_spectrometry` limits already in place). "Many concurrent chemists" reduces
mostly to "many slurm jobs."

**The one hard invariant: nothing blocks the serve's event loop.** With one
serve hosting many sessions, the only thing that breaks multi-user is a
synchronous tool call that stalls the shared process while user A waits, wedging
user B. Two non-negotiables:
1. **`submit_job` is fire-and-return** — submit, return a job id, poll status
   via a *separate* `check_run_status`. No tool call ever holds the serve
   waiting for a job. (Reuses Perspicacité's async-job/SSE pattern.)
2. **Every ssh call has a hard timeout** (the `run_shell` timeout redirect,
   already shipped). A hung ssh fails its own session, never the process.

**Topology: start single-serve, scale by sharding on isolated data dirs.** The
recorded SQLite-contention constraint (§4) is about *shared data dir*, not
multiple processes. So: PoC = one serve, one isolated data dir; scale by adding
serves that each own their own data dir, with users sharded across them by a
router in FastAPI. FastAPI owns the session table
(`cnrs_user_id → shard, opencode_session_id, last_active`), create-on-first-
request, idle-timeout eviction. For a comp-chem-sized population, one serve is
likely sufficient indefinitely; the shard model just avoids ever being trapped.

**Identity, job ownership, data isolation** (the parts that actually bite a
shared HPC backend):
- **Identity:** reuse Perspicacité's CNRS SSO; FastAPI maps the user to shard +
  session. No new auth.
- **Logical job ownership under one PoC service account:** jobs run under
  `spectrometry`, but every `submit_job` is tagged with the CNRS user-id and
  `slurm_job_id → user` is stored in FastAPI, so each chemist sees only their
  own jobs, outputs, and "why" lines. Physical account shared; logical
  ownership enforced in our layer. (Swap for per-user HPC accounts in
  production.)
- **Per-user scratch namespace:** `/scratch/spectrometry/advverum/{user}/{session}/{job}/`
  — prevents cross-user collisions, clean teardown boundary.

**Memory scoping:** project + skill tiers stay **lab-shared** (this *is* the
institutional-learning moat). **Staging is per-user (scope-tagged)** so one
chemist's unvetted lessons don't leak into another's recall until they pass the
promotion gates. Small change: a scope tag on staging + a filter on staging
recall.

**Decisive test (topology fork).** Everything above assumes sessions inside one
serve are isolated under load. That is unverified against opencode serve
internals. The resolving experiment: *two sessions in one serve; Session A fires
a real slow `submit_job`; while it runs, does Session B get prompt→response at
normal latency?* B stays responsive → single-serve-async suffices, shard only
when user count forces it. B stalls behind A → shard pool from day one and push
ssh/submit off the serve thread. Run this before writing the Run tab.

## 10. Honest risks & open questions

- **opencode serve as a multi-user web backend is unproven** (design now in
  §9). CAPSTONE proved the primitives at toy scale (2 agents, trivial tool).
  Still untested: heavy real tools (e.g. `submit_job`), long/branching
  workflows, retry/failure routing, and the §9 single-serve session-isolation
  question. The comp-chem sizing (dozens of users, async work offloaded to
  slurm) makes this far less demanding than a generic multi-user backend, but
  the §9 decisive test still gates the topology choice.
- **Sovereignty is a capability, not yet the default.** Perspicáte defaults to
  DeepSeek; Mimosa recommends Claude/GLM. D1 (sovereign-by-default) is P0 and
  not done.
- **OMO adoption is open.** It ships working multi-agent (potentially retiring
  the flaky raw-opencode-serve path) but is foreign-model-centric + telemetry
  (sovereignty work), and a heavy/volatile external dependency.
- **Resourcing.** Director-level support is confirmed qualitatively, but
  "funded (headcount/budget/pilot cohort)" vs "moral endorsement" is unverified
  — and bifurcates the roadmap.
- **TDM legal clearance** for CNRS-licensed literature (EU 2019/790; needs CNRS
  counsel).
- **Single-lab scale vs CNRS-wide.** A research lab running a helpdesk is an
  operational mismatch; needs real headcount. Mitigated at the PoC bar by the
  comp-chem scoping: the target is CNRS *computational* chemists (a small,
  technical cohort), not all of CNRS Chemistry — a scale a single lab can
  plausibly serve.

## 11. Decisions captured (so far)
- **Name:** Ad Verum (placeholder, Latin).
- **PoC backend:** Azzurra (not Jean Zay — Jean Zay is the production sovereign swap).
- **PoC-1:** haddock3-first (with amber/tleap feeding), gnina → PoC-2 after install.
- **Spine:** Magnolia (operational, disciplined); Mimosa = on-demand Explore, not the headline.
- **Memory:** stays as compchem-memory MCP regardless of orchestration choice.
- **Users:** target is CNRS *computational* chemists (small technical cohort) — sizing that keeps the PoC serve-able by a single lab.
- **Multi-user:** slurm is the concurrency layer; one non-blocking serve, shard by isolated data dir if needed (§9). Sovereign-model verification (Mistral) **deferred** — production target, not a PoC step.

## 12. Next steps
1. Stand up `opencode serve` in an isolated data dir; prove prompt_async/message
   round-trip with compchem MCPs loaded + one real `compchem-tools` call.
2. Write the PoC-1 build spec (Magnolia HTTP-gateway contract + haddock3 Run-tab
   screen + SSE mapping + tleap→haddock3 handoff).
3. Install gnina user-local on Azzurra (in parallel).
4. Run the §9 decisive session-isolation test (two sessions, one real slow
   `submit_job`, latency assertion on the second session) — gates single-serve
   vs. shard-pool before the Run tab is built.
5. (External) Confirm with the CNRS Chemistry directorate: funded vs endorsed;
   convene CNRS IP counsel on TDM rights.
6. (Deferred) Verify the Mistral offering works through Magnolia — production
   sovereign-compute data point, not a near-term PoC step.

---

*References: CAPSTONE memory entry `20260625_115639_901242` (opencode serve
proven); staging `20260701_112742_290097` (opencode-run SQLite contention);
Mimosa arXiv:2603.28986; Perspicacité ISWC-C 2025; `mimosa-magnolia-integration.md`;
`rules/hpc_azzurra.md`. Claude Science / ScienceOne / GPT-Rosalind / Gemini for
Science assessed via their public announcements (2026-06-30 et seq.).*
