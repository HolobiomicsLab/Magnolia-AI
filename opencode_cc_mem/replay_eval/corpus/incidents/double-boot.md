# Incident 3/3 — double boot (circular import)

## Symptom

On 2026-09-17 the memory server launched via `python -m compchem_memory.server`
ran TWO overlapping boot pipelines: two handover merges (83 s and 93 s) and
two distill timers; boot total ~2x normal.

## Root cause

startup_scan's lazy `from compchem_memory.server import RULES_DIR` (~14 s
into boot) re-imported server.py under its canonical name (module was in
sys.modules only as `__main__` under `-m`), re-executing the module body —
a second boot. A module-global boolean cannot guard this (two module objects
do not share globals). Fixed by `storage.resolved_rules_dir()` + env-keyed
`_claim_once` (commit 892614a).

## Expected detection signal

Per-step boot-timing log must show exactly one pipeline of 6 steps per
process start; two `handover` (or two `startup_scan`) steps under one boot
window is the tripwire.
