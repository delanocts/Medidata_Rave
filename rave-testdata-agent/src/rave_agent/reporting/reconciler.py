"""A7 - reconcile what Rave stored against what was submitted (FR-9.1).

An acknowledged submission is not proof a value landed as sent. Rave narrows
formats (a year-only field keeps the year), computes derived fields itself, and
merges a form's fixed section onto every log line. So the report is built from
what Rave returns, not from the tool's own state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from datetime import datetime

from ..config.loader import Config
from ..generation.validators import format_for_rave
from ..model.study_model import StudyModel
from ..rave.client import RaveClient
from ..rave.errors import NotFoundError, RaveError
from ..utils.logging import get_logger
from ..utils.xml import parse_xml

log = get_logger(__name__)

ODM = "http://www.cdisc.org/ns/odm/v1.3"

# Folders that are Rave bookkeeping rather than real visits. Counting them in a
# denominator understates coverage.
PSEUDO_FOLDERS = frozenset({"UNIQUE", "SUBJECT", "EV1"})


@dataclass
class FieldComparison:
    item_oid: str
    folder_oid: str
    form_oid: str
    submitted: str
    stored: str | None
    status: str          # match | normalised | narrowed | mismatch | absent

    @property
    def ok(self) -> bool:
        """Rave reshaping a value it accepted is not a failure."""
        return self.status in ("match", "normalised", "narrowed")


@dataclass
class SubjectReconciliation:
    subject_id: str
    exists: bool = False
    folders_with_data: list[str] = field(default_factory=list)
    folder_form_pairs: int = 0
    form_instances: int = 0
    stored_values: int = 0
    comparisons: list[FieldComparison] = field(default_factory=list)
    error: str = ""

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for comparison in self.comparisons:
            out[comparison.status] = out.get(comparison.status, 0) + 1
        return out

    @property
    def mismatches(self) -> list[FieldComparison]:
        """Values Rave stored differently from what was sent."""
        return [c for c in self.comparisons if c.status == "mismatch"]

    @property
    def absent(self) -> list[FieldComparison]:
        """Values sent that Rave does not hold - a different problem entirely."""
        return [c for c in self.comparisons if c.status == "absent"]


def _stored_values(root) -> tuple[dict[tuple[str, str, str], str], dict[str, Any]]:
    """Index Rave's response by (folder, form, item) and collect shape counts.

    A log form yields one instance per row, so the same item appears many times;
    the first non-empty value wins, which is enough to prove the write landed.
    """
    values: dict[tuple[str, str, str], str] = {}
    folders: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    instances = 0
    populated = 0

    for event in root.findall(f".//{{{ODM}}}StudyEventData"):
        folder_oid = event.get("StudyEventOID") or ""
        folders.add(folder_oid)
        for form in event.findall(f"{{{ODM}}}FormData"):
            form_oid = form.get("FormOID") or ""
            pairs.add((folder_oid, form_oid))
            instances += 1
            for item in form.findall(f".//{{{ODM}}}ItemData"):
                item_oid = item.get("ItemOID") or ""
                value = item.get("Value")
                if value in (None, ""):
                    continue
                populated += 1
                values.setdefault((folder_oid, form_oid, item_oid), value)

    shape = {
        "folders": sorted(folders),
        "folder_form_pairs": len(pairs),
        "form_instances": instances,
        "stored_values": populated,
    }
    return values, shape


def _compare(model: StudyModel, item_oid: str, submitted: Any, stored: str | None) -> str:
    """Classify one field. Rave narrowing a value is not a failure."""
    if stored is None:
        return "absent"

    expected = "" if submitted is None else str(submitted)
    if expected == stored:
        return "match"

    # Rave upper-cases free text on storage. That is normalisation, not loss.
    if expected.upper() == stored.upper():
        return "normalised"

    item = model.items.get(item_oid)

    # Rave re-spells date-times in full ISO 8601: `2025-01-14 09:35` comes back
    # as `2025-01-14T09:35:00`. Same instant, so compare the parsed values.
    if item is not None and item.data_type in ("datetime", "time"):
        if _same_instant(expected, stored):
            return "normalised"

    # Rave pads decimals to the field's significant digits: `0.2` comes back as
    # `0.20`. Numerically identical, so not a loss.
    if item is not None and item.data_type in ("float", "double", "decimal", "integer"):
        if _same_number(expected, stored):
            return "normalised"

    if item is not None and item.is_date_like:
        # The tool generates ISO; Rave stores the field's own format, and a
        # year-only or month-year field keeps just that part.
        rendered = format_for_rave(item, expected)
        if rendered == stored:
            return "match"
        if stored and (stored in expected or expected.startswith(stored.rstrip("-"))):
            return "narrowed"
        # A partial date comes back like "1990--"; compare the parts present.
        parts = [p for p in stored.split("-") if p]
        if parts and all(p in expected for p in parts):
            return "narrowed"

    if item is not None and item.length and len(expected) > item.length:
        if stored == expected[: item.length]:
            return "narrowed"

    return "mismatch"


_DT_LAYOUTS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
    "%H:%M:%S", "%H:%M",
)


def _parse_instant(text: str) -> datetime | None:
    for layout in _DT_LAYOUTS:
        try:
            return datetime.strptime(text, layout)
        except ValueError:
            continue
    return None


def _same_number(sent: str, stored: str) -> bool:
    """True when two differently-formatted numbers are the same value."""
    try:
        return float(sent) == float(stored)
    except (TypeError, ValueError):
        return False


def _same_instant(sent: str, stored: str) -> bool:
    """True when two differently-spelled date-times mean the same moment."""
    left, right = _parse_instant(sent), _parse_instant(stored)
    return left is not None and right is not None and left == right


def reconcile_subject(
    client: RaveClient,
    config: Config,
    model: StudyModel,
    subject_id: str,
    generated_root: Path,
) -> SubjectReconciliation:
    """Pull one subject back from Rave and compare it with what was generated."""
    from rwslib.rws_requests import SubjectDatasetRequest

    result = SubjectReconciliation(subject_id=subject_id)

    try:
        payload = client.send(
            SubjectDatasetRequest(config.study_name, config.environment, subject_id)
        ).value
    except NotFoundError:
        result.error = "subject not found in Rave"
        return result
    except RaveError as exc:
        result.error = str(exc)
        return result

    result.exists = True
    root = parse_xml(payload)
    stored, shape = _stored_values(root)

    result.folders_with_data = shape["folders"]
    result.folder_form_pairs = shape["folder_form_pairs"]
    result.form_instances = shape["form_instances"]
    result.stored_values = shape["stored_values"]

    base = generated_root / subject_id
    if not base.is_dir():
        return result

    for path in sorted(base.glob("*/*.json")):
        if path.name.startswith("_"):
            continue
        folder_oid = path.parent.name
        form_oid = path.stem
        try:
            generated = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        submitted: dict[str, Any] = dict(generated.get("values") or {})
        for record in generated.get("records") or []:
            for item_oid, value in record.items():
                submitted.setdefault(item_oid, value)

        for item_oid, value in submitted.items():
            if value in (None, ""):
                continue
            found = stored.get((folder_oid, form_oid, item_oid))
            result.comparisons.append(FieldComparison(
                item_oid=item_oid, folder_oid=folder_oid, form_oid=form_oid,
                submitted=str(value), stored=found,
                status=_compare(model, item_oid, value, found),
            ))

    log.info("reconciled", extra={
        "subject": subject_id, "stored_values": result.stored_values,
        **result.counts(),
    })
    return result


def coverage(model: StudyModel, reconciliations: list[SubjectReconciliation]) -> dict:
    """Coverage as fractions with stated denominators (FR-9.2).

    Pseudo-folders are excluded: counting Rave's reference matrices as visits
    understates real coverage.
    """
    real_folders = {oid for oid in model.folders if oid not in PSEUDO_FOLDERS}

    writable = [
        item for item in model.items.values()
        if item.visible and not item.derived
    ]

    per_subject = []
    for entry in reconciliations:
        reached = {f for f in entry.folders_with_data if f in real_folders}
        per_subject.append({
            "subject": entry.subject_id,
            "folders_with_data": len(reached),
            "folder_form_pairs": entry.folder_form_pairs,
            "form_instances": entry.form_instances,
            "stored_values": entry.stored_values,
            "field_checks": entry.counts(),
            "empty_folders": sorted(real_folders - reached),
        })

    return {
        "denominators": {
            "real_folders": len(real_folders),
            "pseudo_folders_excluded": sorted(PSEUDO_FOLDERS & set(model.folders)),
            "forms_defined": len(model.forms),
            "fields_defined": len(model.items),
            "fields_writable": len(writable),
            "fields_excluded_derived": sum(1 for i in model.items.values() if i.derived),
            "fields_excluded_not_visible": sum(1 for i in model.items.values() if not i.visible),
        },
        "per_subject": per_subject,
    }
