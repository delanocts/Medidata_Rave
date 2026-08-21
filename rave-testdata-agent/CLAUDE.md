# Rave test data generator - project rules

Spec of record: `docs/requirements.md`. These rules are not optional.

## Safety
- Never target a Production Rave environment. `config/loader.py` refuses any
  environment matching a production pattern, and no config flag unblocks it.
- Never write credentials to disk, logs, prompts, reports or output. Secrets
  come from `.env` only, and are registered for redaction on load.
- TLS verification is always on. There is no supported way to disable it.
- The tool reads study design and never writes it. `PostMetadataRequest` posts
  drafts to Architect and must stay unused.

## Architecture
- All Rave calls go through `src/rave_agent/rave/client.py`. Nothing else may
  talk to RWS directly.
- All XML parsing goes through `src/rave_agent/utils/xml.py`. rwslib mis-decodes
  dataset responses as latin-1 and RWS emits BOMs; the helper repairs both.
- Stages communicate only through on-disk artifacts under `output/<STUDY>/`.
  Each stage has a `scripts/run_*.py` entry point and runs standalone.
- All clinical values come from the LLM. Deterministic code does metadata
  parsing, prompt building, validation, ODM assembly, retries and persistence.

## Study independence
- Never hardcode a study, site, folder, form, field or codelist identifier
  anywhere in `src/`, `config/defaults.yaml`, or any skill. Everything comes
  from config or parsed metadata.
- Adding a study means adding one file under `config/studies/`.
- Before release, grep the tree for the study name; a hit outside
  `config/studies/` and `output/` is a defect.

## This instance (cognizant.mdsol.com, RWS 1.16.0)
- Edit checks and derivations are **not** returned by any endpoint. The version
  metadata carries zero `mdsol:EditCheckDef`. Drafts can be listed but not
  fetched. Dynamics therefore come from a manually exported ALS, or are
  discovered empirically by the A8 loop.
- `mdsol:PrimaryFormOID` gives the subject entry point; never assume a form OID.
- The version metadata declares forms only for the default matrix. Other
  folders' form assignments are recovered by sampling existing subjects
  (`metadata.sample_subjects`), and are marked `source="observed"`.
- `FormDef@Repeating` is `Yes` on every form here and is useless as a log-form
  signal. Use `StudyModel.log_item_groups()` - log lines are repeating
  *item groups*.

## Skills are the source of truth, and some are load-bearing at runtime

`.claude/skills/` holds what this project learned the hard way. Two of them are
read by the running tool, not just by an agent:

- `clinical-data-generation/reference/data-rules.md` is injected verbatim into
  every generation prompt (`generation/skill_rules.py`).
- `rave-submission/reference/error-codes.md` supplies the rejection-to-class
  table the submitter and dynamics loop branch on (`submission/rejections.py`).

Editing those files changes behaviour with no code change. Both degrade to a
built-in fallback if absent, so the tool still runs without `.claude/`.

Before changing how a stage behaves, read its skill. If you learn something new
about Rave or the API, add it to the skill rather than to a comment - that is
what makes it survive.

## Before declaring a stage complete
- Run `pytest tests/unit`. It checks the skills too: frontmatter, name/directory
  agreement, that no skill mentions a study identifier, and that the runtime
  tables still parse.
- Run the stage's own entry point against the real study and read the output.
