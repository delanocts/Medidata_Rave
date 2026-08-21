---
name: clinical-data-generation
description: Build prompts that turn CRF metadata into clinically plausible, metadata-conformant synthetic data. Use when writing or tuning the prompt builder, when generated data is implausible or incomplete, or when steering trigger fields to activate dynamics.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Clinical data generation

## Division of responsibility
Every clinical **value** comes from the LLM. Deterministic code does metadata
parsing, prompt construction, validation, ODM assembly, retries and persistence.
Reformatting a model-supplied value (ISO date to display format) is not
generation; choosing a value is.

## What every field description must carry

Give the model the same contract the validator will enforce - it should not have
to discover the rules by being rejected:

- field OID and its label / question text (prefer the English translation)
- data type, or `date, format YYYY-MM-DD`
- **codelist entries as coded values**, with decodes for meaning, e.g.
  `"1" (Mild), "2" (Moderate)` - and an instruction to return the code, never
  the label
- max length, decimal places, measurement unit
- range constraints
- whether it is required

Cap how many codelist entries you inline; for very long lists, say how many
exist so the model knows it is seeing a subset.

## Completeness is a policy decision

Default prompts let a model leave optional fields blank, which on a real CRF can
mean most of the form - optional fields typically outnumber mandatory ones
several to one.

Expose a `require_all_fields` setting. When on:
- instruct the model to populate **every** field and return no empty strings
- mark every field `required` in the response schema
- make the validator reject any empty value, so the repair loop fixes it

Be honest about the trade: forcing a value into every conditional field
("if other, specify" when the answer is not "other") produces combinations no
real site would enter. That is the right trade for exercising an EDC system and
the wrong one for realistic-looking data. Make it configurable, not implicit.

## Consistency across a subject (FR-6.5)

Generate visit by visit in protocol order and carry a running context into each
prompt: stable demographics, and the dates already used. Without it, each form
is generated in isolation and the subject reads as several different people.

Pass forward the fields that must not drift - sex, birth date, age, race,
ethnicity, height - matched on variable name, plus the visit date already
assigned to each folder.

## Fixed and log sections in one call

A form may have a non-repeating section **and** a repeating log section. Ask for
both in one response - `values` for the fixed part, `records` for the log rows -
and describe each set separately. Generating only the log group leaves the fixed
section empty; generating them in separate calls loses consistency between them.

Note that in Rave the two often occupy the same physical row: values sent to the
fixed group are copied onto every log line. Splitting them for generation is
still correct - it stops the fixed section's mandatory fields being reported
missing on every record - but they are not independent rows.

## Steering dynamics (FR-6.6)

When a field is a known trigger, tell the model the value to use and why. Three
strategies:

- `maximize` - pick the value unlocking the most downstream targets
- `random` - pick among the values that unlock anything
- `as_configured` - only honour explicit config overrides

Config overrides always win over anything the graph suggests, and a pinned value
is not negotiable: re-check it after generation and treat a deviation as a
violation.

`maximize` trades realism for coverage - it will choose an uncommon answer
because that answer opens a matrix. Say so when reporting.

## Log record counts
Ask for an exact number, within the configured min/max, and re-check it in the
validator - the schema cannot pin an array length. Respect any discovered
per-form cap.

## Data hygiene (FR-6.9)
No real names, no initials of real people, no medical record numbers, national
IDs, addresses, phone numbers or emails. State this in the system prompt and
again in the rules.

## Caching
Write each generated form to disk and reuse it unless regeneration is asked for.
A re-run with a warm cache must make no LLM calls at all.

## Constraints
- No study, form or field identifier in this skill or the prompt builder -
  everything is read from the parsed model.
