# Incident set (smoke-detector Phase 0 replay fixtures)

The three historical boot/pipeline faults. Each incident file states the
symptom and the signal a detector should catch. These are the fault-replay
targets for smoke-detector Phase 0 (§6.5 use #2): a detection failure during
replay is a red flag, not a merged change.

- `placeholder-config.md` — placeholder LLM model config silently killed the handover merge.
- `thinking-mode-empty-output.md` — reasoning-mode output landed in the wrong field; extraction saw empty content.
- `double-boot.md` — lazy import re-executed server.py, spawning a second boot pipeline.
