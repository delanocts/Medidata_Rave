"""Turn study metadata into an LLM prompt and a matching JSON schema (FR-6.2, FR-6.6).

The prompt carries every constraint the validator will later enforce, so the
model is told the rules up front rather than discovering them through rejection.
Dynamics conditions are included so trigger fields can be steered (FR-6.6).

No study, form or field identifier is hardcoded here - everything is read from
the model that was parsed from metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model.study_model import Item, StudyModel
from .skill_rules import data_generation_rules

MAX_CODELIST_SHOWN = 40


@dataclass
class FormRequest:
    """Everything needed to generate one form instance."""
    subject_id: str
    folder_oid: str
    folder_name: str
    form_oid: str
    form_name: str
    items: list[Item]                       # fixed (non-repeating) section
    log_items: list[Item] = field(default_factory=list)   # repeating section
    log_group_oid: str | None = None
    record_count: int = 1
    forced_values: dict[str, str] = field(default_factory=dict)
    trigger_notes: list[str] = field(default_factory=list)
    require_all_fields: bool = False

    @property
    def is_log(self) -> bool:
        """True when the form has a repeating section to populate.

        A form may have both a fixed section and a log section; both are
        generated in one call and returned under `values` and `records`.
        """
        return bool(self.log_group_oid and self.log_items)

    @property
    def all_items(self) -> list[Item]:
        return list(self.items) + list(self.log_items)


def _describe_item(model: StudyModel, item: Item) -> str:
    parts = [f'- "{item.oid}"']
    label = item.label or item.name
    if label:
        parts.append(f'label: "{label}"')

    if item.codelist_oid:
        codelist = model.codelists.get(item.codelist_oid)
        if codelist:
            shown = codelist.entries[:MAX_CODELIST_SHOWN]
            rendered = ", ".join(
                f'"{e.coded_value}"' + (f" ({e.decode})" if e.decode else "")
                for e in shown
            )
            if len(codelist.entries) > MAX_CODELIST_SHOWN:
                rendered += f", ... ({len(codelist.entries)} total)"
            parts.append(f"MUST be exactly one of: {rendered}")
    elif item.data_type == "time":
        parts.append("type: time of day, format HH:MM (24-hour) - NOT a date")
    elif item.data_type == "datetime":
        parts.append("type: date and time, format YYYY-MM-DD HH:MM")
    elif item.is_date_like:
        parts.append("type: date, format YYYY-MM-DD")
    else:
        parts.append(f"type: {item.data_type}")
        if item.length:
            parts.append(f"max length: {item.length}")
        if item.significant_digits:
            parts.append(f"decimals: {item.significant_digits}")

    if item.measurement_unit:
        unit = model.measurement_units.get(item.measurement_unit, item.measurement_unit)
        parts.append(f"unit: {unit}")

    for constraint in item.ranges:
        if constraint.values:
            parts.append(f"range: {constraint.comparator} {constraint.values[0]}")

    parts.append("REQUIRED" if item.mandatory else "optional")
    return "  ".join(parts)


def build_form_prompt(
    model: StudyModel,
    request: FormRequest,
    subject_context: dict[str, Any] | None = None,
    therapeutic_area: str | None = None,
    realistic: bool = True,
) -> str:
    """Compose the user prompt for one form."""
    lines: list[str] = []

    lines.append(
        f"Generate clinical trial data for one CRF form in a study of "
        f"{therapeutic_area or 'general medicine'}."
        if realistic else
        "Generate metadata-conformant clinical trial data for one CRF form."
    )
    lines.append("")
    lines.append(f"Subject: {request.subject_id}")
    lines.append(f"Visit/folder: {request.folder_name} ({request.folder_oid})")
    lines.append(f"Form: {request.form_name} ({request.form_oid})")
    lines.append("")

    if subject_context:
        lines.append("Data already recorded for this subject - stay consistent with it:")
        for key, value in subject_context.items():
            lines.append(f"  {key}: {value}")
        lines.append("")

    if request.items:
        lines.append('Fields for the fixed section (return under "values"):'
                     if request.is_log else "Fields:")
        for item in request.items:
            lines.append(_describe_item(model, item))
        lines.append("")

    if request.is_log:
        lines.append(
            f'Repeating log section (return under "records"): produce exactly '
            f"{request.record_count} record(s), each a distinct, clinically "
            "plausible entry with these fields:"
        )
        for item in request.log_items:
            lines.append(_describe_item(model, item))
        lines.append("")

    if request.forced_values:
        lines.append("These fields MUST take exactly these values:")
        for oid, value in request.forced_values.items():
            lines.append(f'  "{oid}" = "{value}"')
        lines.append("")

    if request.trigger_notes:
        lines.append("Notes on fields that drive study workflow:")
        for note in request.trigger_notes:
            lines.append(f"  {note}")
        lines.append("")

    # The clinical rules are supplied by the clinical-data-generation skill, so
    # they can be tuned per deployment without touching this builder. A missing
    # skill file falls back to a built-in set rather than failing.
    lines.append("Rules:")
    lines.append(data_generation_rules())
    lines.append("")

    if request.require_all_fields:
        lines.append("- Populate EVERY field listed. Do not omit any, and do not "
                     "return an empty string for any of them.")
        lines.append("- Where a field would normally only apply in some other "
                     "circumstance, still give the most plausible value for this "
                     "subject rather than leaving it blank.")
    else:
        lines.append("- Omit optional fields only when a real site would plausibly "
                     "leave them blank.")

    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You generate synthetic clinical trial data for testing an EDC system. "
    "The data must be clinically plausible and internally consistent, and must "
    "conform exactly to the field constraints you are given. It is entirely "
    "fictional: never reproduce real patient information. "
    "Return only data conforming to the requested schema - no commentary."
)


def build_form_schema(model: StudyModel, request: FormRequest) -> dict:
    """A JSON schema matching the form, so the response is structurally guaranteed.

    The schema encodes types and codelist enums. It does not replace the
    deterministic validator (FR-6.4), which still checks lengths, ranges, date
    plausibility and cross-field consistency.
    """
    def section(items: list[Item]) -> dict:
        properties, required = {}, []
        for item in items:
            schema = _item_schema(model, item)
            pinned = request.forced_values.get(item.oid)
            if pinned is not None:
                # Make the pinned trigger value structurally the only option.
                # Asking for it in the prose is not enough - the model will
                # sometimes pick another codelist entry, and the run then burns
                # its repair attempts on a value it was told to use.
                schema = {"type": "string", "enum": [str(pinned)]}
            properties[item.oid] = schema
            if (request.require_all_fields or item.mandatory
                    or item.oid in request.forced_values):
                required.append(item.oid)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    if not request.is_log:
        return section(request.items)

    properties: dict[str, dict] = {
        # The API only accepts minItems of 0 or 1 and rejects anything higher
        # ("For 'array' type, 'minItems' values other than 0 or 1 are not
        # supported"), so the exact record count is enforced by the prompt and
        # re-checked by the validator instead.
        "records": {
            "type": "array",
            "items": section(request.log_items),
            "minItems": 1,
        }
    }
    required = ["records"]
    if request.items:
        properties["values"] = section(request.items)
        required.insert(0, "values")

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _item_schema(model: StudyModel, item: Item) -> dict:
    if item.codelist_oid:
        codelist = model.codelists.get(item.codelist_oid)
        if codelist and codelist.coded_values:
            return {"type": "string", "enum": codelist.coded_values}
        return {"type": "string"}

    if item.data_type == "time":
        return {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"}
    if item.data_type == "datetime":
        return {"type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2} ([01]\d|2[0-3]):[0-5]\d$"}
    if item.is_date_like:
        return {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}

    if item.data_type == "integer":
        return {"type": "integer"}
    if item.data_type in ("float", "double", "decimal"):
        return {"type": "number"}

    schema: dict[str, Any] = {"type": "string"}
    if item.length:
        schema["maxLength"] = item.length
    return schema


def build_repair_prompt(violations: list, previous: dict) -> str:
    """Hand the model its own output plus the exact problems found (FR-6.4)."""
    lines = ["The values you returned failed validation against the study metadata.",
             "", "Problems:"]
    for violation in violations:
        lines.append(f"  - {violation.describe()}")
    lines.append("")
    lines.append("Return corrected data for the same fields. Fix only what is listed; "
                 "keep every other value unchanged.")
    return "\n".join(lines)
