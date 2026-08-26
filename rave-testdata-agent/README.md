# rave-testdata-agent

Generates clinically plausible, metadata-conformant test data and loads it into
a Medidata Rave study. Driven entirely by configuration: adding a study means
adding one file under `config/studies/`, with no code changes.

**Non-production only.** The tool refuses any environment that looks like
production, and no configuration flag unblocks it.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env             # then fill it in - git-ignored, never commit it
```

Use the virtual environment rather than a global Python. Several Python 3.14
installations can coexist on Windows, and installing into one while the IDE
runs another produces `ModuleNotFoundError: No module named 'jsonschema'` from
an otherwise working checkout. `.vscode/settings.json` points VS Code at
`.venv`; if your interpreter differs, run `python -c "import sys; print(sys.executable)"`
in the failing terminal and compare it with where `pip` installed.

`.env` holds `RAVE_USERNAME`, `RAVE_PASSWORD` and `ANTHROPIC_API_KEY`. Nothing
else reads credentials, and they are registered for redaction on load.

Create a study config by copying an existing file in `config/studies/`. Set the
Rave host, study name, environment, and the site **number** — which is the ODM
`Location/@OID`, not the site's label.

## Running

```bash
python scripts/run_all.py --study <name>              # everything, in order
python scripts/run_all.py --study <name> --dry-run    # build payloads, post nothing
python scripts/run_all.py --study <name> --resume     # continue after a failure
python scripts/run_all.py --study <name> --stop-after model
```

Every stage is also a standalone script, so anything the orchestrator does is
reproducible one command at a time:

| Stage | Script | Writes to Rave |
|---|---|---|
| A1 connection test | `test_connection.py` | no |
| A2 metadata acquisition | `run_metadata.py` | no |
| A3 study model + dynamics graph | `run_model.py` | no (offline) |
| A4 site and subjects | `run_provision.py` | **yes** |
| A5 data generation | `run_generate.py` | no |
| A6 submission | `run_submit.py` | **yes** |
| A8 dynamics loop | `run_dynamics.py` | **yes** |
| A7 verification and reporting | `run_verify.py` | no |

`run_dynamics.py` generates and submits per pass, so it supersedes running A5
and A6 separately for a full run.

`run_all.py` reports on the subjects *that run* provisioned, taken from
`subjects.json` — not on every subject the output directory has accumulated.
Run `run_verify.py --study <name>` on its own to report on all of them, or pass
`--subject` (repeatable) to pick.

Start with `test_connection.py`. It reports a PASS/FAIL matrix and exits non-zero
on any mandatory failure. The check that matters most is **design-metadata
access**: without it, edit checks cannot be read and dynamics must come from a
manually exported ALS.

## What it needs that RWS will not give you

RWS returns study structure but **not** edit checks or derivations, and drafts
can be listed but not downloaded. Dynamics therefore come from an ALS workbook
exported by hand from Architect. Drop it in
`output/<STUDY>/metadata/` and A2 will pick it up; without one the tool still
runs and discovers dynamics empirically, but cannot predict them.

Architect exports the ALS as SpreadsheetML (often named `.xls`). It is XML, not
a spreadsheet binary — no Excel library is needed.

## Output

```
output/<STUDY>/
  run_manifest.json        stages, outcomes, config hash - drives --resume
  metadata/                downloaded artifacts + provenance manifest
  model/                   study_model.json, dynamics_graph.json, log_limits.json
  generated/<subject>/<folder>/<form>.json
  submissions/<subject>/pass_<n>/{request,response}.xml
  state/<subject>/activation_state.json
  reports/run_<stamp>.{json,html}
  logs/
```

Generated data is cached and reused; a re-run with a warm cache makes no LLM
calls. Pass `--regenerate` to override.

## Configuration worth knowing

| Setting | Effect |
|---|---|
| `generation.require_all_fields` | Populate every writable field, not just mandatory ones. Fills conditional fields a real site would leave blank. |
| `dynamics.trigger_strategy` | `maximize` picks the value unlocking the most targets — deliberately favouring uncommon answers. `random` or `as_configured` trade coverage for realism. |
| `generation.log_records` | Records per log form. Capped by any limit discovered from Rave. |
| `dynamics.custom_function_overrides` | Declare what a custom function activates. Its logic is not derivable from any export. |
| `metadata.sample_subjects` | Sample existing subjects to learn folder/form assignments the metadata omits. Read-only. |
| `execution.max_parallel_subjects` | How many subjects load at once, each in its own process. Subjects are independent, so this is the one axis that genuinely scales; `rave.requests_per_minute` is a study-wide budget and is divided between them. |
| `generation.lookahead_folders` | Generate this many visits ahead of the one being posted. Posting is ~3x generation, so without it a pass costs the sum of both. Speculating wastes work on a visit that turns out absent; the count is reported. `0` restores probe-then-generate. |

## Development

```bash
pytest tests/unit          # 119 tests, no network
```

The suite covers config validation, XML repair, ODM parsing, the ALS postfix
parser, validation rules, the dynamics graph, rejection classification, and a
**second-study regression** (`tests/fixtures/`) proving the parsers key off
structure rather than one study's conventions.

`.claude/skills/` holds what this project learned about Rave, rwslib and the
Claude API. Two are read by the running tool, not just by a human:

- `clinical-data-generation/reference/data-rules.md` — injected into every
  generation prompt
- `rave-submission/reference/error-codes.md` — the rejection-to-class table the
  submitter and dynamics loop branch on

Editing either changes behaviour with no code change. When you learn something
new about Rave, add it to the skill rather than to a comment.

## Documentation

| Where | What |
|---|---|
| `docs/pipeline-guide.html` | Illustrated walkthrough: how the stages fit together, how the dynamics loop detects activation, and the step-by-step for pointing the tool at a new study. Open it in a browser. |
| `CLAUDE.md` | Project rules — the constraints that must hold in any change. |
| `.claude/skills/` | What this project learned about Rave, rwslib and the Claude API, one skill per activity. |

The requirements specification this was built against is held outside the repo;
`CLAUDE.md` is the operative rule set for day-to-day work.

## Safety

- Production environments are refused in code, and again by a pre-tool hook.
- TLS verification cannot be disabled.
- Credentials never reach logs, reports, prompts or output.
- Every request and response is archived for audit.
- All generated data is synthetic and marked as such in the report.
