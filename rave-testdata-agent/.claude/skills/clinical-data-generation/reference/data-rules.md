# Clinical data rules

These rules are injected verbatim into every generation prompt by
`src/rave_agent/generation/skill_rules.py`. Editing this file changes what the
model is told, with no code change.

Keep every rule study-agnostic. Anything specific to one protocol belongs in
that study's config, not here.

## Conformance

- Return the **coded value** for any field with a codelist, never the decode or
  label. `"1"`, not `"Mild"`.
- Dates are `YYYY-MM-DD`. Never abbreviate a month, never use a locale format.
- Respect max length and decimal places exactly.
- Respect stated numeric ranges. A value outside them raises a query in the EDC,
  which is noise.
- Never invent a field. Return only the field OIDs you were given.

## Internal consistency

- A start date is on or before its end date.
- An ongoing event has no end date unless the form requires one.
- Visit dates advance through the protocol; a later visit is never earlier than
  an earlier one.
- Age, birth date and any age-at-event field must agree.
- A "specify" field is only meaningful when its parent answer selects "other";
  when full population is required, still give a plausible value rather than
  leaving it empty.
- A "number of" field must match the number of records actually present.
- Concomitant medication dates fall inside the subject's participation window.
- A medication given for an adverse event starts on or after that event.

## Clinical plausibility

- Vital signs, labs and scores sit in physiologically normal ranges unless the
  form is specifically recording an abnormality.
- Adverse event terms are recognisable clinical terms, not invented ones, and
  severity, seriousness and outcome agree with each other.
- Where the study's therapeutic area is given, choose findings, medications and
  events typical of it. Where it is not, stay general.
- Log records within one form are distinct from one another - not the same entry
  with a changed date.
- Values that a site would derive from other values on the form should agree
  with those values.

## Privacy

The data is entirely fictional. Never produce:

- real personal names, or initials belonging to a real person
- medical record numbers, national identifiers, insurance numbers
- addresses, postcodes, phone numbers, email addresses
- any detail copied from real patient information

Identifiers that a protocol requires (subject number, site number) come from the
tool, not from you.
