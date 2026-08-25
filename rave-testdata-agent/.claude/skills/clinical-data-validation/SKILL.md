---
name: clinical-data-validation
description: Deterministically validate generated clinical values against study metadata before they reach Rave. Use when building or debugging the validator, or when Rave rejects a value that validation passed. This is the correctness guarantee - not the LLM, and not the response schema.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Clinical data validation

## Position in the pipeline
The LLM produces values; this checks them. A response schema reduces repair
round trips but is **not** the guarantee - it cannot express lengths, ranges,
date plausibility or cross-field consistency, and it is dropped entirely when
the API refuses to compile it. Validation must stand alone.

## Never substitute
A violation is described precisely and handed back to the model for repair, up
to a configured retry limit. After that the form **fails with a diagnostic**. Do
not quietly correct a value, drop a field, or fall back to a default - that
hides a metadata misunderstanding and puts unexplained data into a validated
system.

## What to check, in order

1. **Empty**
   - always a violation for a mandatory field
   - a violation for *every* field when full population is requested
2. **Codelist membership** - takes precedence over type. A coded field has no
   free text; compare against the coded value, never the decode/label.
3. **Type**
   - dates: require ISO `YYYY-MM-DD` from the model (unambiguous), then check it
     is a real calendar date and the year is plausible
   - integer: reject non-whole numbers
   - float/decimal: reject non-numeric
4. **Length** - character count for text, digit count for numbers
5. **Range checks** - apply ODM `RangeCheck` comparators (`LT`, `LE`, `GT`,
   `GE`, `EQ`, `NE`). Treat a **soft** breach as a violation too: soft means
   Rave raises a query, which is noise in generated data.
6. **Unknown fields** - a value for a field not on the form is a violation, not
   something to ignore.

## Scope validation to the right field set

A form can have both a fixed section and a repeating log section. Validate a log
record against **the log group's fields only**. Validating it against the whole
form reports the fixed section's mandatory fields as missing on every record -
an infinite repair loop that never converges.

Pass an explicit item scope rather than deriving it from the form.

## Record counts
The response schema cannot enforce an exact array length (the API rejects
`minItems` above 1), so check the count here against what was requested.

## Dates: generate ISO, render at assembly
Ask the model for ISO dates and validate that. Convert to each field's
`mdsol:DateTimeFormat` when building ODM. Translating format tokens
(`yyyy`, `MMM`, `dd`) to strftime must replace longest-first, or `yyyy` is
consumed as `yy` + `yy`. Rave stores month abbreviations upper-cased.

The token dialect is case-sensitive and wider than it first appears. `nn` is
minutes, which frees `mm` to mean **month** - mapping `mm` to minutes renders
every `dd mm yyyy` field as `15 00 2024`. `hh` is the 12-hour clock and pairs
with `rr`, the AM/PM marker; leaving either untranslated emits the literal
`hh:30 rr`. Cover at least: `yyyy yy MMM MM mm dd HH hh nn ss rr`.

**A hyphen glued to the end of a part - `dd- MMM- yyyy`, `MMM- yyyy` - marks
that part as allowed to be unknown.** It is a property of the field, not a
character in the value, and the separator is the space that follows it. Strip
it before translating. Writing it out produces `01- SEP- 2012`, which Rave
accepts into storage and then flags with the system query *"Clinical Data
entered in incorrect format. Please correct."* - the day and year still parse,
the month does not, so the field looks half-entered in the CRF. A hyphen that
is a real separator (`yyyy-MM-dd`) is followed by a letter rather than by
whitespace, so `-(?=\s|$)` distinguishes them.

Enumerate every distinct `mdsol:DateTimeFormat` in a new study's metadata and
render one value through each before a live run. A study using ten formats will
happen to exercise three in the common forms; the rest fail silently, because
a non-conformance query is raised in Rave and reported nowhere in the load
response.

## Exclusions that are not failures
Two field classes cannot be written and must be excluded before generation, not
reported as violations:

- **derived** fields - Rave computes them and refuses any value
- **not-visible** fields - hidden until a visibility action fires

Report the counts so the exclusion is visible rather than silent.

## Constraints
- No study, form or field identifier in this skill or the validator.
- Every rule here must be testable against a fixture with no network access.
