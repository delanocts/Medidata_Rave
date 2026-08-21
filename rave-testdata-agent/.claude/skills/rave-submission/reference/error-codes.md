# Rave rejection classification

Loaded at runtime by `src/rave_agent/submission/rejections.py`. Each row is
`marker | class | note`, matched case-insensitively as a substring of the
rejection text. First match wins, so order matters: put more specific markers
above more general ones.

Classes the submitter understands:

| Class | Meaning |
|---|---|
| `transient` | repeat the same request after a backoff |
| `shrink_records` | too many log rows; drop one and retry |
| `folder_inactive` | the folder is not part of this subject; skip its forms |
| `form_inactive` | folder is live, this form is not in it yet; try other forms |
| `derived_field` | a Rave-computed field was sent; exclude it and regenerate |
| `bad_value` | a value breaks a field rule; repair and retry |
| `payload_shape` | the ODM is malformed; fix the builder, do not retry |
| `permission` | likely an access problem; see rave-permissions-diagnosis |
| `semantic` | anything else; surface, never blanket-retry |

Editing this table changes submitter behaviour with no code change.

<!-- BEGIN RULES -->
```
timed out | transient | network stall
connection reset | transient | network stall
too many requests | transient | rate limited
record restricted by max limit | shrink_records | per-form log cap, undocumented
folder not found | folder_inactive | trigger has not fired yet
folder does not exist | folder_inactive | trigger has not fired yet
does not exist for the subject | folder_inactive | not assigned to this subject
form does not exist in the designated folder | form_inactive | do NOT abandon the folder
form does not exist | form_inactive | do NOT abandon the folder
transaction on derived field | derived_field | Rave computes this field
data not in dictionary | bad_value | value outside the codelist
record does not exist | payload_shape | Update on a log row that is absent; use Upsert
record already exists | payload_shape | Insert onto the auto-created blank row; use Upsert
folder already exists | payload_shape | Insert carrying a seed folder; use SiteRef only
field does not exist | payload_shape | ItemGroupOID likely overwritten with the form OID
filetype | payload_shape | ODM FileType wrong; the rwslib arg is inverted
not a valid odm | payload_shape | schema violation before any semantic check
subject does not exist | payload_shape | create the subject first
study does not exist | permission | on AdminData this is usually access, not naming
```
<!-- END RULES -->
