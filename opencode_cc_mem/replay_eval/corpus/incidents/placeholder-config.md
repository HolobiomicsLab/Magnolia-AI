# Incident 1/3 — placeholder LLM config

## Symptom

Boot-context SESSION HANDOVER stale for weeks; handover merges silently
produced nothing. Logs showed LLM calls "succeeding" while writing no
content (2026-08-28 diagnosis: entry 20260828_150419 family; also the Aug-2026
unrendered `@@DISTILL_MODEL@@` placeholder frozen the handover for 3 weeks).

## Root cause

A placeholder value (`@@DISTILL_MODEL@@` / empty model id) in the rendered
config reached the LLM layer; the call path swallowed the failure
(`call_llm` returns None and callers treat None as "nothing new").

## Expected detection signal

A canary that renders the template, reads the resolved model id, and makes
one REAL cheap LLM call must flag: unresolved `@@...@@` placeholders, empty
model ids, or an LLM call returning None/empty when a key is configured.
