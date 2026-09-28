# Replay-eval harness (EXECUTION_PLAN §6.5)

One frozen corpus of past sessions; any proposed change runs as an **arm**
against it; a **blind judge** scores the outputs; the report lands in
`runs/`. Build once, use four ways:

1. **P4 bake-off** — memory-pipeline changes measured, not debated.
2. **Smoke-detector Phase 0** — `corpus/incidents/` is the fault-replay set.
3. **Retrieval eval** — recall@5/@10 + MRR from `corpus/retrieval/pairs.jsonl`.
4. **Self-evolve gate** — an idea-ticket prototype runs as an arm vs the
   current-master arm; the merge discussion requires the score report.
   Never auto-merge.

## Layout

    corpus/            FROZEN, versioned (manifest.yaml rules: Goodhart guard)
      extraction_gold/ 21 production slices + manifest (capture_version per slice)
      incidents/       3 historical faults with expected detection signals
      retrieval/       query -> expected-memories pairs (pairs.jsonl)
    arms/              arm declarations {name, code_ref, model, prompt_version, flags}
    runs/              one dir per run: outputs/ raw/ judge/ summary.json REPORT.md
    tests/             unit tests (no LLM needed)

## Usage (from opencode_cc_mem/)

Worktree note: point PYTHONPATH at THIS tree's src dirs so arms run this
tree's code, not the live tree's:

    export PYTHONPATH=$PWD/mcp-servers/compchem-memory/src:$PWD/mcp-servers/compchem-tools/src

    python -m replay_eval run --arm replay_eval/arms/baseline-master.yaml [--limit 3]
    python -m replay_eval judge --run replay_eval/runs/<stamp>_<arm> [--model <judge-model>]
    python -m replay_eval report --run replay_eval/runs/<stamp>_<arm>

The judge model should be a different model family than the extractor arms.

## Rules (from the §6.5 spec)

- Arms declare `{name, model ids, prompt_version, flags}`; identical inputs.
- Judge is blind: candidate IDs only, order shuffled, arm names never shown.
- Every corpus change is a version bump with a reason. Never extend a corpus
  after seeing arm results on it — add NEW sessions instead.
- Arms are never compared across capture regimes (`capture_version`).
- Manual invocation only — not a CI gate; boot/perf stays with
  boot-timing.jsonl.

## Known limits

No counterfactual outcomes; model drift pinned by recording model ids per
arm; pre-fix transcripts lack tool outputs (capture-1 regime); human labels
are finite — spend them where a decision is pending.
