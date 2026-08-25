"""Deterministic validation of generated values (FR-6.4).

Every value the LLM produces is checked against the study metadata here. A
violation is described precisely enough to hand back to the model for repair; a
value is never silently corrected or substituted.

The LLM is asked for ISO dates (YYYY-MM-DD) because they are unambiguous. The
conversion to each field's Rave display format happens in `format_for_rave`,
which is deterministic code and so does not count as data generation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from ..model.study_model import Item, StudyModel

# Rave mdsol:DateTimeFormat tokens -> strftime, longest token first so that
# "yyyy" is consumed before "yy" and "MMM" before "MM". Matching is
# case-sensitive, which is what separates `MM` (month) from `mm` (also month,
# in the lowercase dialect some fields use) and `HH` (24-hour) from `hh`.
_FORMAT_TOKENS = [
    ("yyyy", "%Y"), ("yy", "%y"),
    ("MMM", "%b"), ("MM", "%m"), ("mm", "%m"),
    ("dd", "%d"),
    # Rave writes minutes as `nn`; `mm` is always a month, never minutes.
    ("HH", "%H"), ("hh", "%I"), ("nn", "%M"), ("ss", "%S"), ("rr", "%p"),
]

# A hyphen glued to the end of a date part - `dd- MMM- yyyy`, `MMM- yyyy` -
# marks that part as allowed to be unknown. It is a property of the field, not
# a character in the value: the separator is the space that follows it. Emitting
# the hyphen produces `01- SEP- 2012`, which Rave stores but flags with
# "Clinical Data entered in incorrect format", so it is stripped first. A
# hyphen used as a real separator (`yyyy-MM-dd`) is followed by a letter, not
# by whitespace, and is left alone.
_PART_MODIFIER = re.compile(r"-(?=\s|$)")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]([01]\d|2[0-3]):[0-5]\d$")

# A value outside this window is a generation error, not clinical data.
_MIN_YEAR, _MAX_YEAR = 1900, 2100


@dataclass
class Violation:
    item_oid: str
    value: object
    problem: str
    expected: str = ""

    def describe(self) -> str:
        suffix = f" Expected: {self.expected}." if self.expected else ""
        return f"{self.item_oid}: {self.problem} (got {self.value!r}).{suffix}"


def strftime_pattern(rave_format: str) -> str:
    """Translate a Rave DateTimeFormat such as `dd MMM yyyy` to a strftime pattern."""
    out = _PART_MODIFIER.sub("", rave_format or "")
    for token, replacement in _FORMAT_TOKENS:
        out = out.replace(token, replacement)
    return out


_UNTRANSLATED = re.compile(r"(?<!%)[A-Za-z]")


def untranslatable_formats(model: StudyModel) -> dict[str, list[str]]:
    """Date formats this module cannot fully translate, mapped to their fields.

    A token nobody has met before is not an error anywhere: it survives into
    the strftime pattern as a literal, `strftime` returns it unchanged, and
    Rave raises a non-conformance query that appears in no load response. That
    is how `hh:nn rr` reached a live study as `hh:30 rr`. Checking the model up
    front turns a silent per-value failure into one message before the run.
    """
    out: dict[str, list[str]] = {}
    for item in model.items.values():
        if not item.is_date_like or not item.datetime_format:
            continue
        pattern = strftime_pattern(item.datetime_format)
        if _UNTRANSLATED.search(pattern):
            out.setdefault(item.datetime_format, []).append(item.oid)
    return out


def format_for_rave(item: Item, iso_value: str) -> str:
    """Render an ISO date/time into the field's Rave format.

    Values are generated in ISO because it is unambiguous; each field then has
    its own display format (`dd MMM yyyy`, `HH:nn`, ...). Anything that is not a
    date-like field passes through untouched.
    """
    if not iso_value or not item.is_date_like or not item.datetime_format:
        return iso_value

    for layout in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%H:%M"):
        try:
            parsed = datetime.strptime(iso_value, layout)
            break
        except ValueError:
            continue
    else:
        return iso_value  # already flagged by the validator

    pattern = strftime_pattern(item.datetime_format)
    rendered = parsed.strftime(pattern)
    # Rave stores month abbreviations upper-cased (e.g. 20 AUG 2026).
    return rendered.upper() if "%b" in pattern else rendered


# ---------------------------------------------------------------------------
def validate_value(model: StudyModel, item: Item, value: object) -> list[Violation]:
    """Check one value against its ItemDef. Returns every problem found."""
    problems: list[Violation] = []

    if value is None or (isinstance(value, str) and not value.strip()):
        if item.mandatory:
            problems.append(Violation(item.oid, value, "field is mandatory but empty"))
        return problems

    text = str(value).strip()

    # Codelist membership takes precedence: a coded field has no free text.
    if item.codelist_oid:
        codelist = model.codelists.get(item.codelist_oid)
        if codelist is None:
            problems.append(Violation(
                item.oid, value,
                f"references codelist {item.codelist_oid} which is not in the model"))
        elif text not in codelist.coded_values:
            problems.append(Violation(
                item.oid, value, "value is not in the field's codelist",
                expected="one of " + ", ".join(repr(v) for v in codelist.coded_values[:25]),
            ))
        return problems

    if item.data_type == "time":
        problems.extend(_validate_time(item, text))
    elif item.data_type == "datetime":
        problems.extend(_validate_datetime(item, text))
    elif item.is_date_like:
        problems.extend(_validate_date(item, text))
    elif item.data_type in ("integer", "float", "double", "decimal"):
        problems.extend(_validate_number(item, text))
    else:
        if item.length and len(text) > item.length:
            problems.append(Violation(
                item.oid, value, f"is {len(text)} characters, longer than the field allows",
                expected=f"at most {item.length} characters"))

    problems.extend(_validate_ranges(item, text))
    return problems


def _validate_date(item: Item, text: str) -> list[Violation]:
    if not _ISO_DATE.match(text):
        return [Violation(item.oid, text, "is not an ISO date",
                          expected="YYYY-MM-DD, e.g. 2026-03-14")]
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return [Violation(item.oid, text, "is not a real calendar date",
                          expected="a valid YYYY-MM-DD date")]
    if not (_MIN_YEAR <= parsed.year <= _MAX_YEAR):
        return [Violation(item.oid, text, f"year {parsed.year} is not plausible",
                          expected=f"a year between {_MIN_YEAR} and {_MAX_YEAR}")]
    return []


def _validate_time(item: Item, text: str) -> list[Violation]:
    """A time field is HH:MM, not a date.

    Sending a date to one stores garbage - Rave rendered `2024-03-14` into a
    `HH:nn` field as `00:NN`.
    """
    if not _ISO_TIME.match(text):
        return [Violation(item.oid, text, "is not a 24-hour time",
                          expected="HH:MM, e.g. 09:30")]
    return []


def _validate_datetime(item: Item, text: str) -> list[Violation]:
    if not _ISO_DATETIME.match(text):
        return [Violation(item.oid, text, "is not an ISO date and time",
                          expected="YYYY-MM-DD HH:MM, e.g. 2026-03-14 09:30")]
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return [Violation(item.oid, text, "is not a real calendar date",
                          expected="a valid YYYY-MM-DD date")]
    return []


def _validate_number(item: Item, text: str) -> list[Violation]:
    try:
        number = float(text)
    except ValueError:
        return [Violation(item.oid, text, f"is not a valid {item.data_type}",
                          expected=item.data_type)]

    if item.data_type == "integer" and not float(number).is_integer():
        return [Violation(item.oid, text, "is not a whole number", expected="an integer")]

    problems = []
    if item.length:
        digits = len(text.lstrip("-").replace(".", ""))
        if digits > item.length:
            problems.append(Violation(
                item.oid, text, f"has {digits} digits, more than the field allows",
                expected=f"at most {item.length} digits"))
    return problems


def _validate_ranges(item: Item, text: str) -> list[Violation]:
    """Apply ODM RangeCheck constraints. Soft breaches raise a query in Rave, so
    both soft and hard are treated as violations for generated data."""
    if not item.ranges:
        return []
    try:
        number = float(text)
    except ValueError:
        return []  # non-numeric ranges are not enforceable here

    comparators = {
        "LT": (lambda a, b: a < b, "less than"),
        "LE": (lambda a, b: a <= b, "at most"),
        "GT": (lambda a, b: a > b, "greater than"),
        "GE": (lambda a, b: a >= b, "at least"),
        "EQ": (lambda a, b: a == b, "equal to"),
        "NE": (lambda a, b: a != b, "not equal to"),
    }

    problems = []
    for constraint in item.ranges:
        check = comparators.get(constraint.comparator.upper())
        if check is None or not constraint.values:
            continue
        test, phrase = check
        try:
            bound = float(constraint.values[0])
        except ValueError:
            continue
        if not test(number, bound):
            problems.append(Violation(
                item.oid, text,
                f"breaches the {constraint.soft_hard.lower()} range check",
                expected=f"{phrase} {constraint.values[0]}"))
    return problems


# ---------------------------------------------------------------------------
def validate_form(
    model: StudyModel,
    form_oid: str,
    values: dict[str, object],
    require_mandatory: bool = True,
    item_scope: list[Item] | None = None,
    require_all: bool = False,
) -> list[Violation]:
    """Validate a form payload, including unknown-field detection.

    `require_all` demands a value for every field in scope, not just the
    mandatory ones, so a form comes back fully populated.

    `item_scope` narrows validation to a subset of the form's fields. That
    matters for a form with both a fixed section and a repeating log section:
    a log record must be judged against the log group's fields only, or the
    fixed section's mandatory fields are wrongly reported as missing.
    """
    problems: list[Violation] = []
    source = item_scope if item_scope is not None else model.items_for_form(form_oid)
    known = {item.oid: item for item in source}

    for item_oid, value in values.items():
        item = known.get(item_oid)
        if item is None:
            problems.append(Violation(
                item_oid, value, "is not a field on this form",
                expected="one of " + ", ".join(sorted(known)[:15])))
            continue
        problems.extend(validate_value(model, item, value))

    for item_oid, item in known.items():
        supplied = values.get(item_oid)
        empty = supplied is None or (isinstance(supplied, str) and not supplied.strip())
        if not empty:
            continue
        if require_all:
            problems.append(Violation(
                item_oid, supplied, "every field must be populated but this one is empty"))
        elif require_mandatory and item.mandatory and item_oid not in values:
            problems.append(Violation(item_oid, None, "mandatory field is missing"))

    return problems


def validate_records(
    model: StudyModel,
    form_oid: str,
    records: list[dict[str, object]],
    require_mandatory: bool = True,
    item_scope: list[Item] | None = None,
    require_all: bool = False,
) -> list[Violation]:
    """Validate every record of a repeating item group."""
    problems: list[Violation] = []
    for index, record in enumerate(records, start=1):
        for violation in validate_form(model, form_oid, record,
                                       require_mandatory, item_scope, require_all):
            problems.append(Violation(
                f"[record {index}] {violation.item_oid}", violation.value,
                violation.problem, violation.expected))
    return problems
