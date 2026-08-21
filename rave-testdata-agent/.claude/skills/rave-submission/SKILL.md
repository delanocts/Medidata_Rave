---
name: rave-submission
description: POST ODM to Rave and interpret the response. Use when a submission is rejected, when deciding whether to retry, or when a rejection needs attributing to a specific form. Includes a rejection-to-fix table for Rave's terse error strings.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Rave submission

## Inputs
ODM bytes from `odm-authoring`; config for auth, rate limit, retry policy.

## Output
Archived request + response per attempt, and a classified outcome.

## Submit one form per POST

Rave reports **a single reason per transaction**. A whole-visit payload that
fails tells you nothing about which form caused it. Posting form by form
attributes each rejection precisely (FR-7.4) and lets one bad form fail without
taking the visit with it.

The cost is more round trips. Honour the configured rate limit and keep
submissions for a single subject serialised - Rave is stateful per subject.

## Retry only transient failures

Retry: timeouts, connection resets, 429, 5xx.
Never blanket-retry a semantic rejection - surface the field and the reason.

Two semantic rejections are the exception, because they are *recoverable by
changing the payload* rather than by repeating it:

- **`Record restricted by max limit`** - the form's log-record cap. Drop one
  record and retry, down to a single row. The cap is published nowhere, so
  record what worked and reuse it next run.
- **`Schema`/grammar refusals from the LLM side** - see `llm-structured-generation`.

## Rejection to fix

| Rave says | Means | Fix |
|---|---|---|
| `Filetype 'Snapshot' not supported.` | wrong FileType | `ODM(filetype=None)` - the arg is inverted |
| `The 'EffectiveDate' attribute is invalid ... Pattern constraint failed` | datetime where a date is required | emit `%Y-%m-%d` |
| `Field does not exist.` | the field is not in the ItemGroup you named | usually the ItemGroupOID was overwritten with the form OID |
| `Record does not exist.` | `Update` on a log row that is not there | use `Upsert` + repeat key |
| `Record already exists.` | `Insert` onto Rave's auto-created blank first row | use `Upsert` + repeat key |
| `Record restricted by max limit` | too many log records | shrink and retry; cache the discovered cap |
| `Transaction on derived field is not permitted.` | you sent a Rave-computed field | exclude derived fields at generation |
| `Folder already exists.` | `SubjectData/Insert` carrying a seed folder | insert with `SiteRef` only |
| `Folder not found.` | the folder is not active for this subject | a trigger has not fired - see `dynamics-resolution` |
| `Form does not exist in the designated folder.` | folder is live, this form is not in it yet | **do not abandon the folder** - try its other forms |
| `Subject does not exist.` | no `Insert` yet | create the subject first |
| `Data not in dictionary.` | value outside the field's codelist | validate against the codelist before sending |
| `Study does not exist.` on AdminData | usually a **permission** problem, not a name problem | see `rave-permissions-diagnosis` |
| `more than one subject has the same identifier at the given site` | ambiguous subject key | address by a unique key |

**`Folder not found` and `Form does not exist in the designated folder` are not
the same class.** The first means the folder is absent - skip its remaining
forms. The second means the folder is live but this form has not arrived yet -
keep going with the other forms, and retry this one on a later pass. Conflating
them silently drops most of a folder's content.

## Audit trail
Archive the request **before** posting and the response after, so the trail
survives a failure. Keep every attempt, including shrunk retries.

## Constraints
- All calls go through the shared client - never post from an ad-hoc script.
- Refuse to run if config resolves to a production environment.
- Never log credentials or full auth headers.
