---
name: odm-authoring
description: Assemble CDISC ODM payloads for Medidata Rave with rwslib. Use when building AdminData or ClinicalData XML, choosing TransactionTypes, or debugging a payload Rave rejects as malformed. Covers five rwslib defects that silently produce invalid ODM.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# ODM authoring for Rave

## Inputs
`model/study_model.json` for every OID; config for study, environment, site.

## Output
ODM bytes, ready to POST. Never hand-write XML - the builders own element shapes.

## Rule
Every OID comes from the parsed model. If an OID is not in the model, that is a
bug in the model or the request - never invent one to make a payload build.

## rwslib defects you must work around

These produce **valid-looking XML that Rave rejects**. None raise a Python error,
so they only surface against a live instance. Verify each against the installed
version before assuming it is fixed.

### 1. `ODM(filetype=...)` is inverted
```python
self.filetype = ODM.FILETYPE_TRANSACTIONAL if filetype is None else ODM.FILETYPE_SNAPSHOT
```
Passing `FILETYPE_TRANSACTIONAL` yields `FileType="Snapshot"`, which Rave refuses
with `Filetype 'Snapshot' not supported.` **Pass `filetype=None`.**

### 2. `ClinicalData` builds the StudyOID with a space
It composes `"%s (%s)" % (project, environment)`. Many instances address the
study with no space (`STUDY(ENV)`). The authoritative spelling is whatever
`ClinicalStudiesRequest` returns - pass it through verbatim via a subclass that
overrides `build`. Do not re-derive it from project + environment.

### 3. `MetaDataVersionRef` emits a datetime for `EffectiveDate`
It calls `dt_to_iso8601`, producing `YYYY-MM-DDT00:00:00`. The ODM 1.3 schema
types the attribute `xs:date`; Rave fails it on a Pattern constraint. Subclass
and emit `%Y-%m-%d`.

### 4. `FormData.build` overwrites every child's ItemGroupOID
```python
def build(self, builder, formname=None):
    params = dict(ItemGroupOID=formname if formname else self.itemgroupoid)
```
`FormData` passes the **form** OID down as `formname`, so each item group is
emitted with the form's OID instead of its own. Invisible when a form's only
group shares the form's OID; **wrong for every `<FORM>_LOG_LINE` group**, which
Rave rejects with `Field does not exist.`

Subclassing does not work - `ItemGroupData.__init__` calls
`super(self.__class__, self).__init__(...)`, which recurses forever in a
subclass. Patch the method once at import:

```python
def _patch_item_group_oid() -> None:
    original = ItemGroupData.build
    if getattr(original, "_oid_patched", False):
        return
    def build(self, builder, formname=None):
        return original(self, builder, formname=None)
    build._oid_patched = True
    ItemGroupData.build = build
```

### 5. rwslib builds with stdlib ElementTree, not lxml
Serialise with `xml.etree.ElementTree.tostring`. Using `lxml.etree.tostring`
raises `Type 'xml.etree.ElementTree.Element' cannot be serialized`.

## TransactionTypes that actually work

| Goal | Shape |
|---|---|
| Create a subject | `SubjectData TransactionType="Insert"` with **only** a `SiteRef` |
| Write data to an existing subject | `SubjectData/Update` + `StudyEventData/Update` + `FormData/Update` |
| Fixed (non-repeating) item group | `ItemGroupData TransactionType="Update"` |
| Log line | `ItemGroupData TransactionType="Upsert"` **plus an explicit `ItemGroupRepeatKey`** |

Why `Upsert` for log lines: Rave auto-creates a blank first row, so `Insert`
collides (`Record already exists`) and `Update` fails past it
(`Record does not exist`). `Upsert` with an explicit repeat key fills the
existing row and creates the rest.

Do **not** send `SubjectData/Insert` together with a `StudyEventData` for a seed
folder - Rave auto-creates the default matrix's folders on insert, so adding one
gives `Folder already exists.`

## Values

- **Dates**: generate ISO (`YYYY-MM-DD`), then convert to the field's
  `mdsol:DateTimeFormat` at assembly time. That conversion is deterministic code,
  so it does not count as data generation.
- **Derived fields**: never send one. Rave computes them and refuses any value
  (`Transaction on derived field is not permitted`). See `als-parsing` for how
  to find them - the target is often named only by `VariableOID`.
- A field's format may be narrower than a date (e.g. `dataformat: yyyy` stores
  the year only). Send the full date; Rave truncates.

## Constraints
- No study, site, folder, form or field identifier may appear in this skill or
  in the builder module.
- Validate against the local schema before any POST.
