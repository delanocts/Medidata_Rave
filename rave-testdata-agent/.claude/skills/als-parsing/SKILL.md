---
name: als-parsing
description: Parse a Medidata Rave ALS workbook (Architect Loader Specification) into edit checks, derivations, matrices and custom functions. Use when building the study model or dynamics graph, or when an edit check, matrix or derived field looks wrong. Covers the SpreadsheetML format and the postfix condition encoding.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# ALS parsing

## Why the ALS is needed
RWS does not return edit checks or derivations. The metadata endpoint yields
structure only, and drafts can be listed but not downloaded. The ALS is the only
source for the logic that drives dynamics - see `rave-metadata-fetch`.

## The file is XML, not Excel
Architect exports SpreadsheetML 2003 and often names it `.xls`. It is **not** a
binary .xls and **not** an .xlsx: parse it with an XML parser. No openpyxl, no
xlrd. Detect by signature - the file starts with `<?xml`.

Accept `.xls`, `.xlsx` and `.xlsm` when looking for a user-placed workbook, and
sniff the content rather than trusting the extension.

### Sparse rows
SpreadsheetML omits empty cells and uses 1-based `ss:Index` to jump columns.
Expand each row to a dense list honouring that index. Getting this wrong shifts
every column silently - a matrix grid will look plausible and be wrong.

Normalise header names (lowercase, strip non-alphanumerics) so cosmetic header
differences between Rave versions do not break the parser.

## Tabs that matter

| Tab | Carries |
|---|---|
| `Checks` | check name, `Active`, and `infix` - a human-readable rendering of the logic |
| `CheckSteps` | the condition, as a postfix expression |
| `CheckActions` | what fires: action type and target |
| `Derivations` / `DerivationSteps` | fields Rave computes |
| `CustomFunctions` | **function source code**, keyed by name |
| `Matrices` | matrix OID to matrix name, `addable`, `maximum` |
| `Matrix<n>#<OID>` | one grid per matrix: rows are forms, columns folders |
| `Folders` / `Forms` / `Fields` | structural detail and per-field flags |

## CheckSteps are postfix (RPN)

Each step is one of three things:

- **operand - field**: has `FormOID`/`FieldOID` (plus `DataFormat`)
- **operand - literal**: has `StaticValue`
- **operator**: has `CheckFunction` (`IsEqualTo`, `IsNotEmpty`, `And`, `Or`, ...)

Fold them in `StepOrdinal` order onto a stack to rebuild the expression tree.
Track operator arity; unary and binary differ.

To make a condition *actionable* you must invert it into concrete field=value
assignments. Only a pure conjunction of equalities inverts cleanly:

- `And` - recurse into both sides, collect all assignments
- `Or` - several ways to satisfy it; take one branch and **flag the result
  incomplete**
- `IsNotEmpty`, ranges, custom functions - not invertible; flag incomplete

Never present an incomplete condition as if it were fully known. Record the flag
and let the caller decide.

## Action targets are not where you expect

- `AddForm` - target is the **FormOID** column
- `AddMatrix` / `MrgMatrix` - target matrix OID is in **`ActionOptions`**, not a
  folder or form column
- `SetDataPointVisible` - target is FormOID + FieldOID

Matrix sheet names strip punctuation from the OID (`Matrix19#PRIMARY1` for OID
`PRIMARY_1`), so map back through the `Matrices` tab rather than parsing the
sheet name.

## Derivations often name the target only by VariableOID

A `Derivations` row may leave `FormOID` and `FieldOID` blank and identify the
field solely in **`VariableOID`**. Reading only `FieldOID` misses most of them.
Resolve in this order:

1. `FormOID` + `FieldOID` - exact item
2. otherwise match the variable name across every form

Step 2 can over-match when the same variable name exists on several forms. That
is the safe direction (a derived field skipped is better than a derived field
posted, which Rave refuses outright), but record how many were matched loosely
so the over-exclusion is visible.

## Constraints
- No study, form or field identifier may appear in this skill or the parser.
- If a construct cannot be modelled, record it in the unresolvable list rather
  than dropping it.
