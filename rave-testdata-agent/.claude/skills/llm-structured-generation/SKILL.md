---
name: llm-structured-generation
description: Call the Claude API for schema-constrained JSON generation. Use when writing or debugging the LLM client, when a request returns 400, or when deciding how hard to lean on response schemas. Documents the API constraints that break naive clinical-data generation.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write, WebFetch
---

# Structured generation with the Claude API

## Model and parameters

Read the `claude-api` skill for current model ids and the live parameter
surface. The points below are the ones that bite when generating CRF data.

**Do not send `temperature`.** Current Claude models removed the sampling
parameters; sending one returns
`400 invalid_request_error: temperature is deprecated for this model`. Keep the
config key for older model ids if you must, but do not pass it. Variety comes
from the prompt, not from sampling.

## Response schemas: useful, not load-bearing

Constrain responses with `output_config={"format": {"type": "json_schema",
"schema": ...}}`. Build the schema from field metadata: types, codelist enums,
and which fields are required.

It will be refused for some real CRF forms. Both of these are 400s:

- `Schema is too complex`
- `Grammar compilation timed out`

Neither correlates with raw size - a 1.3 KB schema over 22 fields can be
refused. Do not tune around it.

**Handle it by falling back**, not by dropping the form: retry with no
`output_config`, restate the schema inside the prompt, and parse defensively.
Match the fallback on a set of markers (`too complex`, `grammar compilation`,
`output_config.format.schema`) so a new phrasing does not slip through as a hard
failure.

Correctness does not depend on the schema either way. The deterministic
validator is the guarantee - see `clinical-data-validation`.

## Array length cannot be pinned
`minItems` above 1 is rejected:
`For 'array' type, 'minItems' values other than 0 or 1 are not supported`.
Ask for an exact record count in the prompt and re-check it in the validator.

## Parse defensively regardless
The schema path returns clean JSON; the fallback path may add prose or markdown
fences. Strip fences, then take the outermost `{`...`}`. Treat a non-object
response as an error rather than coercing it.

## Other things to handle
- Check `stop_reason == "refusal"` before reading content.
- Accumulate `usage.input_tokens` / `output_tokens` per call so token cost is
  reportable per run.
- Never put credentials, API keys or real patient data in a prompt.

## Repair loop
On validation failure, send back the model's own output plus the exact
violations, and ask for a corrected payload that changes only what was listed.
Cap the attempts; then fail the form with a diagnostic rather than accepting
something invalid.

## Constraints
- This is the only module that talks to the LLM.
- No study, form or field identifier belongs here - it receives a schema and a
  prompt, and knows nothing about the study.
