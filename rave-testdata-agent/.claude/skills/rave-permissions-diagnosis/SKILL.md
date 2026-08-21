---
name: rave-permissions-diagnosis
description: Tell a Rave permission problem apart from a wrong name or a bad payload. Use when RWS returns a misleading error such as "Study does not exist", when site creation fails, or when deciding whether to keep debugging a payload or ask for access.
allowed-tools: Read, Grep, Glob, Bash
---

# Diagnosing Rave permission failures

## The core problem
Rave reports missing **permissions** with the same words it uses for missing
**objects**. `Study does not exist.` on an administrative-data POST usually
means the account may not write administrative data - not that the study name is
wrong. Without a method you can burn a long time re-spelling identifiers.

## Method: prove the payload, then prove the access

Work outward, and stop as soon as one step contradicts the error message.

1. **Is the payload schema-valid?** If Rave complains about an attribute or
   element, it never reached the permission check. Fix the shape first.
2. **Does the error survive every plausible spelling?** Try the study addressed
   with and without environment, with and without a space, in the casing the
   study list reports. Identical failure across all of them is not a naming bug.
3. **Does the shape Rave itself emits also fail?** Fetch the equivalent
   read-only dataset and copy its structure exactly. If Rave rejects its own
   shape, the payload is not the problem.
4. **Can the same account do the analogous thing elsewhere?** A clinical-data
   POST that reaches *semantic* processing proves auth, connectivity and study
   addressing are all fine. A semantic complaint ("more than one subject has the
   same identifier") is a **success** for this purpose.
5. **Can the account read the same object class?** Reading administrative data
   while failing to write it isolates the gap to a write permission.
6. **Flush the cache and retry.** Rave caches permissions; a freshly granted
   right may not be visible yet.

Reaching step 6 with the error unchanged means the permission is genuinely
missing, or the feature is disabled on the instance. You cannot tell those apart
from outside - say so rather than guessing.

## Report it so someone can act
State what was tried and what it proved:
- payload validated (earlier schema errors are the evidence)
- N identifier spellings, all identical
- the instance's own shape also refused
- clinical writes succeed on the same study
- reads of the same object class succeed
- cache flushed

Then name the specific right needed, and note the alternative - do the action in
the UI, or point config at an object that already exists.

## Objects invisible to RWS are often unassigned, not missing
A site created in Rave but not yet added to the study with a CRF version does
not appear in the site dataset at all, because that dataset is driven by
study-site associations. Before concluding an object was not created, check
whether it simply is not linked to the study.

## Watch for number-versus-name confusion
Rave's site *number* is the ODM `Location/@OID`; the *name* is a label. Local
conventions can make these look reversed. Config must hold whatever the OID is,
since that is what every reference carries. Read the existing objects and follow
their pattern rather than assuming.
