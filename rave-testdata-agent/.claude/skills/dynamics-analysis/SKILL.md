---
name: dynamics-analysis
description: Build the activation graph that says which field values unlock which folders, forms and fields. Use when constructing or debugging dynamics_graph.json, or when deciding what a trigger field should be set to. Read als-parsing first - it produces the input.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Dynamics analysis

## What the graph is
Nodes are folders, forms, item groups and fields. An edge means *this target is
activated when these field values hold*. Edges come from, in precedence order:

1. config overrides (including declared custom-function effects) - always win
2. ALS edit-check actions
3. nothing else - RWS does not expose edit checks

## Which actions activate

Only a few action types activate anything. The rest raise queries, add comments,
send mail and so on - they are not edges.

| Action | Activates |
|---|---|
| `AddForm` | a form |
| `AddMatrix` / `MrgMatrix` / `OldMrgMatrix` | a matrix |
| `SetDataPointVisible` | a field |
| `CustomFunction` | unknowable from metadata - see below |

A matrix edge is not the end of the story: merging a matrix brings in every
folder and form that matrix declares. Expand it, so the graph names the folders
and forms a trigger really unlocks rather than an opaque matrix OID.

## Conditions

An edge's condition is a set of field=value assignments that must all hold, plus
a completeness flag. Mark it incomplete when the source expression contained an
`Or` or an operator that cannot be inverted into a concrete value - satisfying
the listed values may not be sufficient. Carry that flag through to the report;
do not present a partial condition as certain.

Index edges by every field their condition mentions, so a trigger field can be
looked up directly, and build a `field -> {values that unlock something}` map to
drive the `maximize` strategy.

## Classify every target (FR-3.5)

`static`, `dynamic_by_edit_check`, `dynamic_by_derivation`,
`dynamic_by_matrix_add`, or `unresolvable`.

Anything reachable but unexplained must appear in the unresolvable list with a
reason and a hint - never dropped. Typical entries:

- a matrix no activating action targets
- a folder reachable only through such a matrix
- a form assigned to no folder in any matrix and named by no `AddForm`

## Custom functions

A `CustomFunction` action's effect is not derivable from its steps. If the ALS
carries a `CustomFunctions` tab, keep the source in the graph so a human can
read it, but do not try to execute or infer from it.

Provide a config block where the effect can be declared explicitly - trigger
field, trigger value, and what it activates - and let those declarations
override anything inferred.

## Cycles and depth
Detect activation cycles and cap traversal depth. A cycle is not necessarily a
defect - report it rather than failing.

## Expect one dominant trigger
In practice a small number of fields unlock most of a study - often a single
eligibility or enrolment flag that merges the main visit matrix. Report the most
powerful trigger fields with the count of targets each unlocks; it tells the
user what one value is worth and makes the `maximize` strategy legible.

## Constraints
- No study, folder, form or field identifier in this skill or the graph builder.
- Everything is derived from the parsed ALS and study model.
