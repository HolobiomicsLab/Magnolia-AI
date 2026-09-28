# Incident 2/3 — thinking-mode empty output

## Symptom

Extraction/distillation candidates came back empty or unparseable;
`tokens_out` hit the cap while `content` stayed empty; report synthesis
returned nothing ("unparseable" fallbacks) on thinking-enabled models.

## Root cause

Reasoning models put output in `reasoning_content` and exhaust the token
budget before emitting `content`. Calls that do not send
`thinking: {"type": "disabled"}` (or that budget content after reasoning)
return empty content with a "complete" finish.

## Expected detection signal

A canary call with `disable_thinking=True` and a known-answer prompt must
return non-empty parseable content; empty `content` with `tokens_out` at the
cap is the tripwire.
