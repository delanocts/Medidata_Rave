---
name: study-model-building
description: Turn ODM metadata plus matrix and ALS sources into the normalized study model every later stage consumes. Use when building or debugging study_model.json, or when a form, field, codelist or log section looks wrong. Covers Rave-specific ODM traps.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Building the study model

## Output
`model/study_model.json` - the contract for generation, submission and the
dynamics loop. Stable, human-readable, and free of study-specific code.

## Order of assembly
1. Parse the ODM for the active CRF version - structure and codelists.
2. Merge the matrix map for folders no version metadata declares.
3. Merge observed structure from existing subjects, where available.
4. Merge ALS matrix grids - the most authoritative source.
5. Mark derived fields.
6. Resolve the subject entry point.
7. **Only then** compute coverage warnings.

Warnings computed mid-pipeline describe a half-built model and will be wrong.
Defer them to a final pass.

## Rave-specific traps

### `FormDef@Repeating` is not a log-form signal
Studies commonly set it on every form. Log lines are **repeating item groups**
(`ItemGroupDef@Repeating`). Derive log-ness from the item groups a form
contains, and expect forms that have both a fixed group and a log group.

### Item OIDs are `FORM.VARIABLE`
The form OID is the prefix. Use it to associate items with forms, but validate
that the implied form actually has a `FormDef` - record a warning if not.

### Question text is multilingual
`Question/TranslatedText` repeats per language. Select by `xml:lang`, falling
back to the first entry - do not take the last one you happen to iterate.

### Codelist order is an mdsol attribute
Sort entries by `mdsol:OrderNumber`, not document order.

### Mandatory lives on the reference, not the definition
`ItemRef@Mandatory` inside the item group, not `ItemDef`. The same item can be
mandatory in one group and not another.

### Not-visible fields exist
`mdsol:Visible="No"` marks fields hidden until a visibility action fires. They
are candidates for dynamic activation and must be excluded from generation until
then.

## Provenance matters
Tag every folder-form assignment with where it came from - declared metadata,
ALS, or observation - and never let a weaker source overwrite a stronger one.

An **observed** assignment proves a form *can* appear in a folder, not that it
is there at baseline; it may arrive only when a matrix merges. Treating
observation as seed-time truth causes confident submissions that Rave refuses.

## The subject entry point
The CRF version names it in `mdsol:PrimaryFormOID`. Never hardcode a form OID.

Which folder it is filed under is a separate question, and the metadata may not
say. Prefer the placement seen on existing subjects (most frequent, preferring a
seed folder), then a seed folder that declares the form. Record the choice and
the alternatives.

Be aware the entry form may not be writable at all: some studies create the
subject from a `SubjectData/Insert` carrying only a site reference, and refuse a
post of the primary form because it belongs to no seed folder.

## Referential integrity
Record dangling references - unknown codelist, unknown measurement unit, form
with no definition - as warnings. Keep the object; do not drop it.

## Constraints
- No study, folder, form or field identifier in this skill or the parsers.
- Adding a study means adding one config file and nothing else.
