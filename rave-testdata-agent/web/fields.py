"""The configurable surface, described once and rendered by the browser.

Every entry here is a real key in `config/config.schema.json`. The form is
generated from this list rather than hand-written in HTML, so a field cannot
drift away from the schema that validates it - a mismatch shows up as a
validation error on save instead of a silently ignored input.

`always` marks a key that is written to the study file even when it matches the
default, because a study file that does not name its own study, environment or
site is not readable on its own.
"""
from __future__ import annotations

GROUPS = [
    {
        "title": "Rave",
        "note": "Where the data goes. Production environments are refused in code; no setting here unblocks that.",
        "fields": [
            {"key": "rave.base_url", "label": "Base URL", "type": "text", "always": True,
             "placeholder": "https://your-instance.mdsol.com",
             "help": "Must be https. TLS verification cannot be turned off."},
            {"key": "rave.environment", "label": "Environment", "type": "text", "always": True,
             "help": "Must be one of rave.allowed_environments - DEV, Sandbox, UAT, Training."},
            {"key": "rave.requests_per_minute", "label": "Requests per minute", "type": "number",
             "min": 1, "max": 600,
             "help": "A budget for the whole study. Parallel subjects divide it between them, so raising subject concurrency does not raise load on Rave."},
            {"key": "rave.timeout_seconds", "label": "Timeout (s)", "type": "number", "min": 5, "max": 600},
            {"key": "rave.max_retries", "label": "Max retries", "type": "number", "min": 0, "max": 10},
        ],
    },
    {
        "title": "Study",
        "fields": [
            {"key": "study.name", "label": "Study name", "type": "text", "always": True,
             "help": "Exactly as Rave spells it, without the environment suffix."},
            {"key": "study.crf_version", "label": "Pin CRF version", "type": "text",
             "help": "Leave empty to use the version assigned to the site, which is what data entry there is judged against. A pin overrides that."},
        ],
    },
    {
        "title": "Site",
        "fields": [
            {"key": "site.number", "label": "Site number", "type": "text", "always": True,
             "help": "The ODM Location OID, not the site's label."},
            {"key": "site.name", "label": "Site name", "type": "text", "always": True},
            {"key": "site.country", "label": "Country", "type": "text", "always": True,
             "help": "ISO 3166 alpha-2, e.g. US."},
            {"key": "site.create_if_missing", "label": "Create the site if missing", "type": "checkbox",
             "help": "Needs rights to upload administrative data. Most accounts do not have them."},
        ],
    },
    {
        "title": "Subjects",
        "fields": [
            {"key": "subjects.count", "label": "How many", "type": "number", "min": 1, "max": 500, "always": True},
            {"key": "subjects.id_prefix", "label": "ID prefix", "type": "text", "always": True},
            {"key": "subjects.id_start_number", "label": "Start numbering at", "type": "number",
             "min": 1, "max": 100000, "always": True},
            {"key": "subjects.id_pad_width", "label": "Digits", "type": "number", "min": 1, "max": 10},
            {"key": "subjects.on_existing_id", "label": "If the ID already exists", "type": "select",
             "options": ["skip", "continue_numbering", "fail"]},
        ],
    },
    {
        "title": "Generation",
        "note": "What the model is asked for, and how hard the tool works to fill a form.",
        "fields": [
            {"key": "generation.model", "label": "Model", "type": "text"},
            {"key": "generation.therapeutic_area", "label": "Therapeutic area", "type": "text",
             "help": "Free text. The more specific, the more plausible the values."},
            {"key": "generation.therapeutic_realism", "label": "Clinically realistic values", "type": "checkbox"},
            {"key": "generation.require_all_fields", "label": "Populate every writable field", "type": "checkbox",
             "help": "Not just the mandatory ones. Fills conditional fields a real site would leave blank."},
            {"key": "generation.max_tokens", "label": "Max tokens per form", "type": "number",
             "min": 1000, "max": 64000},
            {"key": "generation.max_values_per_form", "label": "Max values per form", "type": "number",
             "min": 10, "max": 2000,
             "help": "Fields times log records. A wide log form otherwise asks for hundreds of values in one reply and the answer is truncated."},
            {"key": "generation.max_parallel_forms", "label": "Forms generated at once", "type": "number",
             "min": 1, "max": 16},
            {"key": "generation.lookahead_folders", "label": "Visits generated ahead", "type": "number",
             "min": 0, "max": 4,
             "help": "Generates the next visit while this one posts. 0 restores strict probe-then-generate."},
            {"key": "generation.max_validation_retries", "label": "Repair attempts", "type": "number",
             "min": 0, "max": 10},
            {"key": "generation.log_records.min", "label": "Log records, min", "type": "number", "min": 1, "max": 100},
            {"key": "generation.log_records.max", "label": "Log records, max", "type": "number", "min": 1, "max": 100},
            {"key": "generation.enrolment.first_date", "label": "First subject enrols", "type": "date",
             "help": "Each subject's Day 1 is derived from its ID across the window below, so the cohort spreads out and a regenerated subject keeps its dates."},
            {"key": "generation.enrolment.window_days", "label": "Enrolment window (days)", "type": "number",
             "min": 0, "max": 3650},
            {"key": "generation.cache", "label": "Reuse generated data", "type": "checkbox",
             "help": "A re-run with a warm cache makes no LLM calls."},
        ],
    },
    {
        "title": "Dynamics",
        "note": "Most of a study is invisible to a new subject until an edit check fires.",
        "fields": [
            {"key": "dynamics.enabled", "label": "Resolve dynamic folders", "type": "checkbox"},
            {"key": "dynamics.trigger_strategy", "label": "Trigger strategy", "type": "select",
             "options": ["maximize", "random", "as_configured"],
             "help": "maximize picks the answer unlocking the most targets, deliberately favouring uncommon ones."},
            {"key": "dynamics.max_iterations", "label": "Max passes", "type": "number", "min": 1, "max": 20},
        ],
    },
    {
        "title": "Metadata and execution",
        "fields": [
            {"key": "metadata.sample_subjects", "label": "Existing subjects to sample", "type": "number",
             "min": 0, "max": 100,
             "help": "Read-only. Learns folder and form assignments the version metadata omits."},
            {"key": "execution.max_parallel_subjects", "label": "Subjects loaded at once", "type": "number",
             "min": 1, "max": 16,
             "help": "Subjects are independent, so this is the one axis that genuinely scales."},
            {"key": "execution.log_level", "label": "Log level", "type": "select",
             "options": ["DEBUG", "INFO", "WARNING", "ERROR"]},
        ],
    },
]


def all_fields() -> list[dict]:
    return [f for g in GROUPS for f in g["fields"]]
