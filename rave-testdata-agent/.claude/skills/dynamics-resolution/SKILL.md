---
name: dynamics-resolution
description: Drive the iterative loop that activates dynamic folders and forms, then populates them. Use when building or debugging the A8 loop, when folders never activate, or when deciding how to detect that something became active.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Dynamics resolution loop

## The detection problem

You cannot detect activation by reading. The clinical dataset endpoint returns
only folders that **already hold data**, so a folder that is active but empty is
invisible - which is exactly the state you are trying to detect.

**Use the write as the detector.** Attempt to post into a candidate folder:
acceptance proves it is active, a structural refusal proves it is not. That also
removes the need for a separate probe, since the data you wanted to write is the
probe.

## The loop

1. **Pass 0** - generate and submit the seed set (the default matrix's folders).
2. **Each later pass** - collect every value written so far, ask the graph which
   folders those values should have unlocked, and attempt the ones not yet
   fully populated.
3. **Stop** when a pass activates nothing new and submits nothing, or the
   configured iteration cap is reached.

Persist state per subject per pass so the loop is resumable and auditable, and
so a re-run does not redo completed work.

Never re-submit a form already written.

## Classify refusals correctly - this is the easy bug

Two refusals look similar and mean different things:

| Refusal | Meaning | Response |
|---|---|---|
| `Folder not found` | the folder is not part of this subject | skip its remaining forms this pass |
| `Form does not exist in the designated folder` | folder is live, this form has not arrived | **continue to the next form**, retry this one later |

Treating the second like the first abandons a live folder after its first
missing form and silently drops most of its content. Keep them in separate
marker lists and test both paths.

Anything else is a value problem, not an activation problem - hand it to
`rave-submission` for classification.

## Evaluating conditions

An edge fires when **every** assignment in its condition is satisfied by the
values written so far. Conditions flagged incomplete (they contained an `Or` or
a non-invertible operator) should still be attempted - satisfying the listed
values may be enough - but expect some of them not to activate.

Expand a matrix target into the folders and forms that matrix declares, so the
loop knows what a merge actually brings in.

## Report predicted versus actual (FR-8.7)

Every folder the graph predicted but that never accepted data must be listed
with a likely reason:

- the condition needs a value not generated (an unmet `Or` branch)
- a custom function drives it, so its logic was never derivable
- the folder needs a later visit or a workflow state the run does not reach
- permissions

Silence here is the failure mode: a study can look fully populated while a third
of its folders were never reachable.

## Per-form caps
A form's log-record maximum is published nowhere. Discover it by being refused,
shrink the record list, retry, and cache what worked so later runs and later
subjects stay inside it. Share that cache with the generator.

## Posting dominates a pass - overlap it
Rave charges per message, not per value. A measured pass: 308 posts carrying
3,383 values in total - a median of **10 fields each** - cost 50.7 minutes,
against 15.4 minutes of generation. Posts of 1-5 fields still averaged 5.4
seconds, and field count explained almost none of the latency (r = 0.30).

Left strictly alternating - generate a visit, post it, generate the next - the
two phases never overlap and a pass costs their sum. Generating one visit ahead
of the one being posted hides the generation entirely.

The tension is with the probe: a visit prepared before its probe answers is
wasted work if the visit turns out absent. Both are right, so make the depth a
setting, keep the probe as the zero case, and **count the discarded forms** so
the trade is visible in the run's own output rather than inferred from a bill.

Batching many forms into one message looks like the bigger win and usually is
not. A batched message is all-or-nothing: one bad form rejects the whole thing
and forces a per-form retry of that visit, and it destroys the per-form
attribution the loop needs to tell "form not in this folder" from "folder not
active". Measure first - in the same run, 15 of 19 visits contained at least one
rejection and held 48.8 of the 50 minutes, so batching would have fallen back to
per-form on everything that mattered.

## Constraints
- One subject's submissions stay serialised; parallelism is across subjects.
- Generation may run ahead of posting; posting order may not change.
- No study, folder or form identifier in this skill or the resolver.
