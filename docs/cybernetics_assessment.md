# Magnolia through the Lens of Engineering Cybernetics

## An Expert Panel Assessment

**Date:** 2026-04-17 (updated 2026-04-22)
**Framework:** Qian Xuesen's *Engineering Cybernetics* (1954) applied to agentic system design
**Scope:** Magnolia v2 memory system — three-tier hierarchy, MCP tool servers, health checks, notebook/lab-log

---

## Gap Checklist

### Critical

- [x] **2.1 Open feedback loop: run outcomes do not influence future behavior**
  - Fixed. Auto-assessment fires mechanically via `magnolia-run` after every command.
  - `record_run` stores `overall` status and `quality_flags`.
  - **Run outcomes now feed into retrieval scoring:** `_load_recent_run_outcomes()` reads `.magnolia/runs/*.yaml` and maps tool → worst recent status (fail/warning/pass). `_score_entry()` applies 2.0x boost to `error_resolution`/`failure_pattern` entries for tools with recent failures, and 0.7x penalty to `success_pattern` entries for failed tools.
- [x] **2.2 No persistent reference signal (project goal)**
  - Fixed. GOAL.md added to `.magnolia/` with MCP tools (memory_set_goal, memory_get_goal). Loaded first in context assembly as reference signal.
- [x] **2.3 Unbounded state growth with no true forgetting**
  - Fixed. Consolidator now enforces max_entries cap by archiving lowest-confidence entries to `.magnolia/archive/`. `archive/` subdir auto-created.

### High

- [x] **2.4 Confidence only increases, never decreases**
  - Fixed. Two mechanisms now provide negative confidence feedback:
  - Confidence decay in `_score_entry()` — entries lose relevance if `last_verified` is old (half-life ~60 days).
  - **Negative confidence feedback:** `ProjectManager.decrement_confidence_for_tool()` decrements confidence on `success_pattern` entries when a tool run fails. Called from `post_run_assess` and `cmd_assess`. Only applies to entries not recently verified (older than 7 days). Small decrement (-0.05), floored at 0.
- [ ] **2.5 Keyword-only retrieval is a severe bottleneck**
  - Not implemented. `_score_entry` still uses word-splitting only.
- [x] **2.6 Context assembly loads tiers in wrong precedence order**
  - Fixed. Skills now loaded first (30% protected floor), then session, run, project entries last. Project entries fill remaining budget.

### Medium

- [ ] **3.1 No adaptive learning rate**
  - Not implemented. All thresholds are hardcoded constants.
- [x] **3.2 No `failure_pattern` entry type**
  - Fixed. `failure_pattern` added to ENTRY_TYPES with type_boost=2.5. Distiller now extracts unresolved failures from session logs.
- [ ] **3.3 No session handoff mechanism**
  - Not implemented. No auto-generated summary between sessions.
- [ ] **3.4 Single-session auto-promotion risk**
  - Not implemented. Observations can still all come from one session. auto_promote_staging uses observation_count without cross-session check.
- [x] **3.5 Consolidation destroys knowledge structure**
  - Fixed. `_merge_duplicates` now properly merges frontmatter (max confidence, sum observation_count, union tags/tools) instead of raw file concatenation. `_expire_stale` uses parse-modify-write instead of fragile string replacement. Keeper written before duplicates are unlinked.
- [ ] **3.6 No undo or versioning for destructive operations**
  - Not implemented. No backup-before-mutate mechanism.
- [ ] **3.7 Agent's decision rationale is invisible**
  - Not implemented. Session log does not record why the agent chose an action.
- [ ] **3.8 No skill-project contradiction detection**
  - Not implemented. Health check does not compare skill rules against project evidence.

### Implementation Bugs Found During Review

- [x] **post_run_assess data loss** — `record_run` derived status from exit_code, ignoring assessment `overall` and `quality_flags`. Fixed: now passes assessment `overall` as status.
- [x] **Dead conversation_history path** — `memory_get_context` never passed `conversation_history` to `assemble_context`, making the `recent_tools` diversity filter unreachable. Fixed: parameter now wired through.

---

## Panel Composition

Three independent experts analyzed the full Magnolia codebase, each through a different cybernetics lens:

| Expert | Perspective | Focus Areas |
|--------|-------------|-------------|
| Control Systems Engineer | Classical control theory | Feedback loops, stability, observability, controllability, reference signals |
| Adaptive Systems Researcher | Self-improving AI architectures | Learning rates, forgetting, self-tuning, error-driven learning, consolidation |
| AI Systems Architect | Hierarchical agentic design | Tier decomposition, information flow, autonomy boundaries, fault tolerance, scalability |

---

## 1. What Magnolia Does Well

All three experts independently identified the same architectural strengths.

### 1.1 Three-Tier Hierarchy Maps to Correct Time Constants

The session (ephemeral JSONL), project (durable Markdown entries), and skill (authoritative, human-gated) tiers have clearly differentiated lifespans. This matches hierarchical control theory's principle that higher levels should operate on slower timescales with broader scope.

- **Session tier** — operational, high-frequency, resets per conversation
- **Project tier** — tactical, medium-frequency, evolves with project
- **Skill tier** — strategic, low-frequency, human-reviewed reference knowledge

The skill tier's `last_verified` field and the project tier's `confidence`/`observation_count` fields enforce this separation — skills are reviewed knowledge, project entries are evolving hypotheses, session events are raw telemetry.

### 1.2 Staging Area as a Low-Pass Filter

New learnings are written to staging by default (`memory_record_learning`). Promotion requires either explicit human confirmation (`memory_confirm`) or three observations with confidence above 0.85 (`auto_promote_staging`). This prevents single-observation noise from polluting durable knowledge. The `observation_count` acts as an integrator that only fires when a threshold is crossed.

### 1.3 Health Check as a Calibration Cycle

The six-check system in `health.py` (stale entries, low confidence, orphaned entries, duplicates, broken references, stalled staging) targets actual degradation patterns. The dry-run-by-default with `fix=False` is correct engineering — inspect before mutate.

### 1.4 Budget-Aware Context Assembly as Anti-Windup

`context_assembly.py` allocates token budgets across tiers (session 20%, runs 10%, project 40%, skill 30%) and truncates content to fit. This prevents any single tier from monopolizing context — analogous to anti-windup in a PID controller where the integrator is clamped to prevent saturation.

### 1.5 Post-Run Assessment as a Measurement Transducer

`post_run_assess` in `assessor.py` independently verifies outputs: checks exit codes, verifies output files exist, extracts metrics, and applies quality heuristics. The system does not rely on the LLM's self-report alone. This is a properly closed measurement path.

### 1.6 Project-Local Storage

Storing `.magnolia/` inside the project directory (rather than a central database) means the knowledge base rsyncs, gits, and transfers with the project. This is critical for computational chemistry where projects move between workstations and HPC clusters.

### 1.7 Human-in-the-Loop at Critical Boundaries

Skill promotion requires explicit human invocation. The health check defaults to `fix=False`. Rules files are human-authored. This prevents fully autonomous corruption of long-term knowledge — appropriate for a domain where the cost of wrong knowledge is high.

---

## 2. Critical Gaps

All three experts converged on the same set of weaknesses. They are presented here ordered by severity.

### 2.1 Open Feedback Loop: Run Outcomes Do Not Influence Future Behavior

**Severity: Critical**
**Principle: Closed-loop feedback**

`post_run_assess` records metrics and quality flags into run history and session logs, but nothing in the system feeds those metrics back into the parameters of the next run. HADDOCK3 scores, cluster counts, and quality flags sit inert in YAML files. The context assembly pipeline can retrieve past run records, but there is no mechanism that maps measurements to control actions.

The LLM agent must spontaneously decide to consult run history and apply the lessons — this is an open-loop reliance on the agent's initiative. In Qian's framework, the feedback path from plant output to controller input is open: the system measures the output but does not close the loop through a transfer function.

#### Information Flow Diagram (with file locations)

All paths below are relative to a project directory, e.g. `projects/my_project/.magnolia/`.

```
 Run 1 (e.g. HADDOCK3 docking)
 ═════════════════════════════

 ① User says: "Dock this peptide onto my protein"
    │
    ▼
 ② memory_get_context(task_description="dock peptide onto protein")
    │
    │  context_assembly.py:assemble_context() is called
    │
    ├── SESSION TIER (loaded first)
    │       reads: .magnolia/sessions/2026-04-17.jsonl
    │       takes: last 20 events
    │       budget: 20% of token budget (max 6000)
    │
    ├── PROJECT TIER (loaded second)
    │       reads: .magnolia/entries/*.md
    │           scanned by: scanning.py → scan_memory_headers()
    │       scored by: retrieval.py → _score_entry()
    │           formula: type_boost × (0.5 + confidence × 0.5)
    │                    × (1.2 if observations >= 3)
    │                    + word overlaps with title/desc/tags/tools
    │       re-ranked by: retrieval.py → llm_select_memories() (top-15 → top-5)
    │       budget: ~40% of remaining (max 12000)
    │       returns: top 5 entries + full content
    │
    └── SKILL TIER (loaded last)
            reads: ~/.magnolia/skills/*.md   (global, not project-local)
            scored by: word overlap with tool name + description
            budget: whatever remains (max 8000)
            returns: matching skill files + content
    │
    │  Assembled context is returned to the LLM agent
    ▼
 ③ LLM agent reads context, decides parameters, calls HADDOCK3
    │
    │  Tool call is logged:
    │      writes: .magnolia/sessions/2026-04-17.jsonl  (append event)
    │
    ▼
 ④ HADDOCK3 runs → produces output files in a run directory
    │
    │  Run directory created:
    │      writes: runs/haddock3_peptide_20260417/
    │
    ▼
 ⑤ post_run_assess(run_dir, tool="haddock3")
    │
    │  assessor.py:assess_run() is called
    │
    │  ├── _technical_check()
    │  │       reads: runs/haddock3_peptide_20260417/output/*/io.json
    │  │       checks: expected output files exist?
    │  │
    │  ├── _extract_metrics()
    │  │       reads: runs/.../output/*_caprieval/capri_clt.tsv
    │  │       reads: runs/.../output/*_clustfcc/clustfcc.txt
    │  │       extracts: score=-45.2, fnat=0.32, cluster_count=4
    │  │
    │  ├── _quality_heuristics()
    │  │       checks: positive HADDOCK score? no clusters? very low fnat?
    │  │       produces: quality_flags=[] (clean run)
    │  │
    │  └── overall: "pass"
    │
    │  Assessment is recorded to TWO locations:
    │      writes: .magnolia/runs/haddock3_peptide_20260417.yaml   ← durable
    │              (tool, exit_code, metrics, quality_flags, overall)
    │
    │      writes: .magnolia/sessions/2026-04-17.jsonl             ← append event
    │              (event_type: "run_assessment", metrics, overall)
    │
    ▼
 ⑥ Agent reports result to user. Session ends.


 ═══════════════════════════════════════════════════════════════════════
  THE GAP: where the loop is open
 ═══════════════════════════════════════════════════════════════════════

 Run 2 (same project, next day)
 ═════════════════════════════

 ① User says: "Try docking again with different restraints"
    │
    ▼
 ② memory_get_context(task_description="dock with different restraints")
    │
    │  context_assembly.py → retrieval.py → _score_entry()
    │
    │  Scoring formula (retrieval.py lines 145-177):
    │  ┌──────────────────────────────────────────────────────────┐
    │  │  score = type_boost[type]            # hardcoded: 1.0-2.5│
    │  │  score *= (0.5 + confidence × 0.5)   # from YAML header  │
    │  │  if observations >= 3: score *= 1.2  # only increments   │
    │  │  for word in task_words:                                  │
    │  │      +5.0 if word in entry title                          │
    │  │      +2.0 if word in entry description                    │
    │  │      +3.0 if word in entry tags                           │
    │  │      +4.0 if word in entry tools                          │
    │  └──────────────────────────────────────────────────────────┘
    │
    │  ⚠️  What's NOT in the scoring formula:
    │
    │      The run assessment at these locations:
    │          .magnolia/runs/haddock3_peptide_20260417.yaml  ← has metrics
    │          .magnolia/sessions/2026-04-17.jsonl            ← has events
    │
    │      Is NEVER connected to:
    │          .magnolia/entries/*.md                          ← scoring targets
    │
    │  Specifically missing from _score_entry():
    │      - No "this entry was injected for a run that passed"  signal
    │      - No "this entry was injected for a run that failed"  signal
    │      - No "this entry's advice produced score -45 (good)"  signal
    │      - No "this entry was retrieved 10x, 8 runs failed"   signal
    │      - No entry_age factor
    │      - No confidence decay over time
    │
    ▼
 ③ Same entries retrieved, same scoring weights, no learning from Run 1


 ═══════════════════════════════════════════════════════════════════════
  DATA EXISTS BUT IS DISCONNECTED
 ═══════════════════════════════════════════════════════════════════════

  WHAT EXISTS TODAY:                    WHAT'S MISSING:

  .magnolia/entries/X.md                No record of WHICH entries
  (entry content + frontmatter)         were injected into context for
        │                               which run
        ▼
  context_assembly.py                   No feedback FROM run outcome
  (assembles context)                   TO entry scoring
        │
        ▼
  .magnolia/runs/haddock3_....yaml      Nobody reads this back to
  (run metrics + quality_flags)         adjust retrieval weights
        │
        ▼
  .magnolia/sessions/2026-04-17.jsonl   Last 20 events loaded as
  (all session events)                  raw text, no structured query

  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─

  The missing link: a record in .magnolia/entries/X.md frontmatter like:

      retrieval_count: 10
      retrieval_outcomes:
        pass: 2
        fail: 8
        warning: 0

  Or a separate file like .magnolia/retrieval-stats.yaml that tracks
  which entries were injected for which runs and what the outcomes were.
```

### 2.2 No Persistent Reference Signal (Project Goal)

**Severity: Critical**
**Principle: Reference signal / setpoint**

`memory_get_context` accepts a `task_description` string, which is the closest thing to a reference signal. But this is ephemeral — it comes from the current prompt and is not persisted. There is no "project goal" or "active hypothesis" that the system tracks and validates decisions against.

A control system requires a reference signal that persists and is compared against the measured output to generate an error signal. Magnolia has no persistent reference. Each task is treated independently. The system cannot detect that it has drifted away from the user's overall research intent because that intent is never encoded as a durable state variable.

### 2.3 Unbounded State Growth with No True Forgetting

**Severity: Critical**
**Principle: Bounded integrator / graceful degradation**

Entries are marked `stale: true` but never archived or removed. The `max_entries=50` parameter exists in the consolidator function signature but is **never enforced**. Retrieval cost grows linearly with entry count. The `_score_entry` function in `retrieval.py` does not factor in entry age or staleness. A 90-day-old entry with a stale flag still gets full scoring weight.

Over months of operation, the entries directory grows monotonically. Eventually, the O(n) scan plus the `max_files=200` truncation in `scan_memory_headers` means older entries become invisible. This is not a crash, but a slow degradation of retrieval quality as the system "forgets by dilution."

### 2.4 Confidence Only Increases, Never Decreases

**Severity: High**
**Principle: Bidirectional feedback / stability**

`find_similar_staging` only bumps `observation_count` upward. The `bump_observation_count` method increments by 1, never decrements. There is no path for "we observed this pattern but it led to a bad outcome, so decrease confidence."

If a pattern is initially tagged as a success but later turns out to be harmful, the system has no mechanism to reduce its score. The confidence + observation_count system is a pure integrator with a unidirectional input — it has no negative feedback.

### 2.5 Keyword-Only Retrieval Is a Severe Bottleneck

**Severity: High**
**Principle: Observability — a hierarchical controller is only as good as its sensors**

The retrieval system in `retrieval.py` uses word-level token matching (`task_words = set(task_lower.split())`). There is no embedding, no synonym expansion, no concept matching. A task description of "run molecular dynamics simulation" will not match a project entry titled "GROMACS equilibration failed — missing position restraints" because there are zero word overlaps.

The LLM-based re-ranking (`llm_select_memories`) exists but only runs on the top-15 candidates from the keyword stage, so it cannot recover entries that the keyword filter already excluded. This is the single most important bottleneck in the information flow.

### 2.6 Context Assembly Loads Tiers in Wrong Precedence Order

**Severity: High**
**Principle: Hierarchical precedence**

In `context_assembly.py`, the `allocate_budget()` function loads: session first, then runs, then project entries, then skills last. Skills are loaded last and only get whatever budget remains.

In Qian's framework, the skill tier (strategic) should be loaded first and should constrain what the agent considers valid. If the agent is about to run a HADDOCK3 calculation and the skill file says "ligand params MUST be in ALL CNS modules," this constraint needs to be present in context before the agent plans its approach — not after project entries about past runs have consumed the budget.

---

## 3. Additional Gaps (Medium Severity)

### 3.1 No Adaptive Learning Rate

The extraction threshold (5000 tokens, 3 tool calls), the promotion threshold (3 observations, 0.85 confidence), and the health check thresholds (90-day staleness, 0.3 minimum confidence) are all hardcoded constants. Magnolia treats a 3-session-old project and a 300-session-old project identically.

In adaptive control, the adaptation gain must be scheduled based on the state of the estimator (gain scheduling). When parameter estimates are uncertain (early project), the gain should be high. When estimates have converged (mature project), the gain should decrease.

### 3.2 No `failure_pattern` Entry Type

The ENTRY_TYPES tuple is `("success_pattern", "error_resolution", "parameter_guidance", "workflow_note", "note")`. There is no type for recording "this approach does NOT work." Failed experiments that were not resolved are invisible to the knowledge base.

In robust adaptive control, the cost function must penalize errors more heavily than it rewards successes (asymmetric loss). Magnolia can learn "HADDOCK3 worked with sampling=1000" but cannot learn "HADDOCK3 failed with sampling=500 and no restraints."

### 3.3 No Session Handoff Mechanism

Each session creates a new JSONL file. There is no summary or state vector carried between sessions. The session tier — which holds the richest event data — is ephemeral. `memory_distill_session` bridges this gap, but it is opt-in and writes to staging, adding latency.

This is like a controller that loses its integrator state every time the power cycles. The project and skill tiers persist, but operational continuity across sessions is weak.

### 3.4 Single-Session Auto-Promotion Risk

`auto_promote_staging` promotes entries when `observation_count >= 3` AND `confidence > 0.85`. But observation counts are bumped by the LLM itself via `find_similar_staging` — there is no requirement that the observations come from different sessions. An LLM making the same mistake three times in one conversation will generate three bumps and auto-promote an incorrect entry.

### 3.5 Consolidation Destroys Knowledge Structure

`_merge_duplicates` concatenates the full text of duplicate entries with `\n\n---\n\n`. This does not merge frontmatter — it keeps the first file's metadata and appends the second file's full content. If the two entries had different confidence values or tags, the merged entry is internally inconsistent.

Similarly, `_expire_stale` inserts `stale: true` by doing string replacement on the first `---` delimiter, which produces malformed YAML if the frontmatter already has a `stale` key.

### 3.6 No Undo or Versioning for Destructive Operations

`promote_to_skill` deletes the source entry. `confirm_staging` moves the file. Consolidation deletes duplicates. Health check with `fix=True` modifies files. There is no backup, no versioning, and no undo mechanism. Only the compaction module creates `.backup.jsonl` files.

### 3.7 The Agent's Decision Rationale Is Invisible

The session log records `event_type`, `tool`, `args`, and `result_summary`. But it does not record the agent's reasoning — why it chose a particular tool, what alternatives it considered, or whether it consulted memory before acting. A critical state variable — the agent's decision rationale — is unobservable.

### 3.8 No Skill-Project Contradiction Detection

Once a rule is in the skill tier, there is no mechanism to flag it as outdated or to detect when project-tier evidence contradicts it. If the skill file for HADDOCK3 says "sampling 1000" but project entries show "sampling 2000 produced better scores," this contradiction is invisible.

---

## 4. Scalability Concerns

**Will hold up:**
- File-per-entry architecture (Markdown + YAML frontmatter) scales to hundreds of entries
- Run history (one YAML per run) is fine for dozens of runs
- Session logs (append-only JSONL with compaction) manages growth
- `scanning.py` has a `max_files=200` limit that prevents unbounded reads

**Will not hold up:**
- `list_entries` reads every file's full text to parse frontmatter. At 500+ entries, this becomes a latency bottleneck on every retrieval call. There is no cached index.
- `_update_related_links` is O(n²) — for each new entry, it reads and potentially rewrites every existing entry.
- `_check_orphaned_entries` in the health module is also O(n²).
- The notebook generator scans all files every time with no caching.
- The system has **no persistent index**. Every retrieval, search, and context assembly operation re-scans the filesystem.

---

## 5. Human-in-the-Loop Assessment

**Well-placed boundaries:**
- `memory_promote` (project → skill) requires explicit invocation
- `memory_annotate` creates entries directly in the active area, bypassing staging
- Health check defaults to `fix=False`
- Rules files are entirely human-authored

**Gaps:**
- The user has no visibility into what auto-extraction is proposing to staging. There is no "pending staging entries" view.
- Session compaction silently rewrites session logs, keeping only the last 5 events plus a summary. The user is not notified that context has been destroyed.
- `memory_distill_session` proposes entries to staging but there is no mechanism for the user to **reject** them — only confirm. A "reject staging entry" tool would complete the feedback loop.
- There is no mechanism for the user to set runtime guard conditions like "stop if the HADDOCK score exceeds -10" or "alert me before spending more than 30 minutes."

---

## 6. Ranked Recommendations

| # | Recommendation | Principle Addressed | Difficulty | Impact |
|---|---------------|---------------------|------------|--------|
| 1 | **Close the run-outcome feedback loop** — after `post_run_assess`, write comparative entries (better/worse than prior runs) that influence future retrieval scoring | Closed-loop feedback | Medium | Critical |
| 2 | **Add a persistent project goal** (`GOAL.md`) — always injected into context, provides a reference signal for drift detection | Reference signal / setpoint | Easy | Critical |
| 3 | **Implement confidence decay** — `effective_confidence = confidence × exp(−λ × age_days)`, boosted by successful retrievals; factor into `_score_entry` | Graceful forgetting / bounded state | Easy | High |
| 4 | **Add `failure_pattern` entry type** — distiller creates these for unresolved errors; high retrieval priority when task matches | Error asymmetry | Easy | High |
| 5 | **Restructure context assembly** — load skills first (with protected floor budget), then project, then session | Hierarchical precedence | Easy | High |
| 6 | **Enforce entry budget** — archive stale entries to `archive/` subdirectory; enforce `max_entries` that is currently ignored | Bounded integrator | Easy | High |
| 7 | **Add session handoff summary** — auto-generate at session end, inject at next session start | State continuity across resets | Easy | Medium |
| 8 | **Multi-session confirmation for auto-promotion** — require observations from ≥2 different sessions before auto-promotion | Independence of evidence | Easy | Medium |
| 9 | **Bidirectional confidence** — decrement confidence when injected context leads to failed runs | Negative feedback | Medium | Medium |
| 10 | **Skill-project contradiction detection** — health check flags when project evidence contradicts a skill rule | Hierarchical consistency | Medium | Medium |

---

## 7. Big Picture

Magnolia has a sound architectural skeleton: hierarchical state, staged promotion, budget management, and periodic calibration. These are the markings of a system designed with an intuitive understanding of feedback.

The critical weakness is that several important feedback paths are **open**. The system measures run outcomes but does not feed them back into parameter selection. It injects context but does not measure whether the context was used. It accumulates knowledge but has limited ability to prune or correct it. The reference signal (user intent) is ephemeral rather than persistent.

The highest-impact interventions are:
1. Close the run-outcome feedback loop
2. Add a persistent goal as a reference signal
3. Enforce bounded state with true expiration and confidence decay

These three changes would transform the system from a sophisticated open-loop recorder into a genuine closed-loop controller that improves its own performance over time.

The good news: most recommendations are additive, backward-compatible changes. The system does not need to be rewritten — it needs its feedback paths closed.

---

## Appendix: Key Files Examined

- `server.py` — MCP tool surface (623 lines)
- `health.py` — Health check subsystem (349 lines)
- `tiers/project.py` — Project-tier manager (413 lines)
- `tiers/session.py` — Session-tier manager (100 lines)
- `tiers/skill.py` — Skill-tier manager (103 lines)
- `storage.py` — Storage resolution (86 lines)
- `notebook.py` — Lab notebook generator (246 lines)
- `learning/consolidator.py` — Consolidation/merger (138 lines)
- `learning/assessor.py` — Post-run assessment (164 lines)
- `learning/distiller.py` — Session distillation (79 lines)
- `context_assembly.py` — Context assembly pipeline (158 lines)
- `retrieval.py` — Semantic retrieval (191 lines)
- `compaction.py` — Session compaction (243 lines)
- `extraction.py` — Automatic memory extraction (264 lines)
- `scanning.py` — Header scanning (101 lines)
- `llm.py` — LLM integration (51 lines)
- `README.md` — Project documentation
- `WORKFLOW_GUIDE.md` — User workflow guide
- `rules/` — Domain-specific tool protocols
