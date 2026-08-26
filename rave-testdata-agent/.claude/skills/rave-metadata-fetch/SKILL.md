---
name: rave-metadata-fetch
description: Retrieve study design metadata from Rave Web Services with cache-skip. Use when downloading ODM metadata, matrices, sites or subjects, or when deciding whether a needed artifact is obtainable from RWS at all. Documents what RWS does not expose and how to recover it.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Rave metadata acquisition

## Resolve the version from the site, then check it again before writing
The version a study is judged against is the one assigned to the *site*, not the
study's newest - a site left on an older amendment has different forms, fields
and dictionaries, and posting the newest against it is rejected field by field.

Resolving it once is not enough. The stage that resolves it and the stages that
use it are separated by a file on disk, and the resolving stage is skipped on a
resume or a partial run - so the model can be older than the site it describes,
and nothing notices. Re-read the assignment from the live response at the point
of first write, compare, and **refuse** rather than warn: a run against the wrong
version does not fail, it silently produces a subject full of rejected fields.

Keep one copy of the matching rule and give it the parsed document, not a path:
one stage reads the file it downloaded, the other the response it already holds,
and two copies of the rule drift apart exactly where this check is meant to bite.

## Output
`output/<study>/metadata/`, plus a manifest recording source URL, retrieval
time, SHA-256 and study version per artifact.

## What to fetch

| Artifact | Endpoint (rwslib class) | Notes |
|---|---|---|
| CRF versions | `StudyVersionsRequest` | newest first; pin via config or take latest |
| Drafts list | `StudyDraftsRequest` | tells the user which draft to export as ALS |
| ODM metadata | `StudyVersionRequest` | structure for one version |
| Matrix map | `VersionFoldersRequest` (`datasets/VersionFolders.odm`) | **the only place all matrices appear** |
| Sites | `SitesMetadataRequest` (`datasets/Sites.odm`) | |
| Subjects | `StudySubjectsRequest` | feeds ID-collision policy |

## What RWS will not give you

Verify per instance, but expect all of these:

- **No ALS export.** No endpoint returns the workbook. Accept a user-placed file
  and log that acquisition was manual.
- **No draft download.** Drafts can be listed; fetching one returns
  `RWS URL does not exist`.
- **No edit checks or derivations** in the version metadata. rwslib can *build*
  `mdsol:EditCheckDef` because those belong to the draft-upload format; the
  download side does not return them. Check by counting
  `mdsol:EditCheckDef` / `mdsol:DerivationDef` in the response - if zero, the
  ALS is the only route.
- **No log-record maximum** per form, and no per-form activation rules.

## The version metadata describes only the default matrix

`StudyVersionRequest` returns every `FormDef` and `ItemDef`, but folder-to-form
assignments **only for the default matrix**. Folders reachable through other
matrices appear in `VersionFolders.odm` with no forms attached.

Merge sources in increasing order of authority:

1. **observed** - sampled from existing subjects; inferred
2. **version metadata** - declared, default matrix only
3. **ALS matrix grids** - declared, every matrix

Record which source each assignment came from. It matters later: an *observed*
assignment proves a form **can** appear in a folder, not that it is there at
baseline - it may only arrive once a matrix merges.

## Recovering structure by observing subjects

When a study already has subjects, sampling them recovers assignments no
metadata declares. This is read-only; keep the folder/form/item-group shape and
discard every value.

Choose subjects that will actually teach you something: the subject list carries
workflow flags, so prefer `Empty != "Yes"` and `Touched != "No"`, and spread
picks across the list rather than taking the first N (subjects cluster by site).

Counts observed this way are a **lower bound**, never a cap. A form whose
sampled subjects each hold one log line may still allow many.

## Cache skip
Skip an artifact when it exists on disk and its SHA-256 still matches the
manifest. Log the skip. A `--force-download` flag overrides. Re-running with a
warm cache must make no network calls.

## Two parsing traps
- Responses may carry a UTF-8 BOM **and** an `encoding="utf-8"` declaration,
  which XML parsers reject.
- rwslib does not set `response.encoding` on dataset endpoints, so requests
  falls back to latin-1: the BOM arrives as three characters
  (`U+00EF U+00BB U+00BF`) rather than `U+FEFF`, and every other non-ASCII byte
  is mangled the same way. Re-encoding such a string with latin-1 recovers the
  original bytes exactly.

Route every parse through one helper that handles both, plus leading whitespace.

## Constraints
- Never mix studies: one output folder per study name.
- Record provenance for every artifact, including manual ones.
