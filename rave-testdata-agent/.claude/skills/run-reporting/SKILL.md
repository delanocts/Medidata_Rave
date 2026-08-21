---
name: run-reporting
description: Reconcile what was submitted against what Rave actually stored, and report coverage honestly. Use when building the run report, or when deciding what counts as success for a data-generation run.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Run reporting

## Reconcile against Rave, not against intent
Read the subject's data back and compare field by field with what was
submitted. A submission Rave acknowledged is not proof the value landed as sent:
formats are narrowed (a year-only field keeps the year), derived fields are
computed, and log rows may be merged into a form instance.

## Report coverage as a fraction, never as a count alone
"53 forms populated" means nothing without the denominator. Report:

- forms populated / forms assigned to active folders
- fields populated / writable fields on those forms
- folders active / folders in the study
- **writable** field count, with derived and not-visible fields excluded and
  those exclusions stated

A run can look complete while most of the study was never reachable.

## Always report predicted versus actual
List every target the dynamics graph predicted that never activated, with the
likely reason. Also report conditions flagged incomplete - an `Or` or a
non-invertible operator means the listed values may not be sufficient, and those
are the ones most likely to have failed.

## Report the trade-offs the run made
- which trigger strategy was used, and that `maximize` picks uncommon values to
  open matrices
- whether full-population mode was on, and that it fills conditional fields a
  real site would leave blank
- per-form log caps discovered by being refused
- validation repair attempts, and any form that failed after them

## Include cost and provenance
Token usage per run, wall-clock duration, config hash, metadata hash, CRF
version, and which source supplied each folder-form assignment.

## Mark the data
Every report states that the data is synthetic. Nothing in the report may
contain a credential.

## Exit code
Non-zero when a mandatory stage failed. A partially covered run is a reportable
outcome, not a crash - one subject failing should not lose the others.
