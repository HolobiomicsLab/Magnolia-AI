# Router prototype — decisions log (2026-11-03)

**All choices recorded here are PROVISIONAL and subject to change in future
iterations.** They are good-enough defaults for a quick demonstrable prototype,
not ratified architecture. The full design space and open questions remain in
`dsh-magnolia-v2-router-assessment.md` (section 22), which is itself an
assessment for review, not approval to implement.

| # | Choice made today | Status | Notes / reversal path |
|---|---|---|---|
| 1 | **SQLite control store** per tree, one writer (ControlEngine) | PROVISIONAL | Assessment §16 Phase-1 default. Store is swappable; KGMemory is explicitly not this store. |
| 2 | **Work Item state names** per assessment §8.2 (`candidate → authorized → queued → running → completed/failed/waiting_human/stopped/ignored`) | PROVISIONAL | Mapping to legacy inbox states (`done/error`, `open/answered/closed`) still undecided (§15.5). |
| 3 | Legacy mailbox lifecycle untouched | PROVISIONAL | Prototype does not integrate with the mailbox at all; adapter-vs-migrate decision deferred. |
| 4 | **Approval = Option A (operator-terminal)**: single-use nonce + content hash binding; approver identity is the interactive operator | PROVISIONAL | Receipt format reserves room for Option B (signed artifacts) later; free-text approver strings never accepted. No web panel. |
| 5 | **Future live pilot = hsc70** with an existing approved protocol | USER DECISION, provisional scope | Nle protocol decoupled from the pilot critical path (sequence/linker/IP-FP unresolved). Live pilot still needs real-Slurm and independent-reviewer acceptance tests. Prototype uses only the fake adapter. |
| 6 | **KGMemory deferred** — no graph code in this prototype | PROVISIONAL | Evidence links keep stable IDs so a later projection can be built retroactively (Phase 6). KGMemory repo access (404) unresolved. |
| 7 | Memory-drift policy: **record + targeted warn, not blanket block** | PROVISIONAL, out of prototype scope | `verify_memory_state()` remains uncalled in production; fixing that is Phase 0 work, not this branch. |
| 8 | Quality/efficiency thresholds | DEFERRED BY DESIGN | Set after a baseline replay corpus exists; not invented now. |

## Prototype scope decisions (also provisional)

- New branch `exp/router-prototype` off `v2-dev`; new package `router_runner/`
  rather than growing `campaign_runner` or `magnolia-agents-daemon`.
- Threads/Subscriptions/forum are omitted; a Trigger carries its source and
  project hint directly. The demo's two projects are `literature` (letter
  source) and `hsc70` (work target).
- The fake protocol `hsc70-met-proxy-v1` stands in for a real pre-registered
  protocol; it exists only to exercise protocol-hash binding.
- The §15 baseline defects (wait_for filename mismatch, non-terminal release,
  uncalled memory-state check, cross-project write guards, lifecycle mismatch)
  are **not** fixed by this branch; they remain Phase 0 work.
- The assessment document itself is committed alongside this prototype so the
  design context travels with the code.
