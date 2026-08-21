# Regression fixtures

Two structurally different studies (SK-3). Neither shares an identifier
with the other or with any real study, so passing both proves the parsers
key off structure rather than one study's conventions.

| | study_a | study_b |
|---|---|---|
| OID style | UPPER | lower-with_underscore |
| Codelist values | `Y`/`N` | `1`/`0`, and a numeric severity list |
| Log form | none | `ae_log` with a `_LOG_LINE` group |
| Hidden field | none | `scr_entry.hidden_flag` |
| Derived field | none | `calc_bmi`, named only by VariableOID |
| Question language | en only | de before en |
| Date formats | `dd MMM yyyy` | `yyyy-MM-dd`, `dd MMM yyyy`, `HH:nn` |
| Dynamics | none | MrgMatrix + AddForm, plus a query-only action |
