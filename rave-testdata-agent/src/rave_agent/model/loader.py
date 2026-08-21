"""Rehydrate study_model.json into a StudyModel (ARC-1).

Stages after A3 read the model from disk rather than re-parsing metadata, so the
artifact really is the contract between stages.
"""
from __future__ import annotations

import json
from pathlib import Path

from .study_model import (
    CodeList,
    CodeListEntry,
    Folder,
    Form,
    FormAssignment,
    Item,
    ItemGroup,
    Matrix,
    RangeConstraint,
    StudyModel,
)


def _only_known(cls, payload: dict) -> dict:
    """Drop keys the dataclass does not declare, so an older artifact still loads."""
    allowed = set(cls.__dataclass_fields__)
    return {k: v for k, v in payload.items() if k in allowed}


def load_model(path: Path) -> StudyModel:
    if not path.is_file():
        raise FileNotFoundError(f"study model not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("crf_version") or {}

    model = StudyModel(
        study_name=raw.get("study_name", ""),
        environment=raw.get("environment", ""),
        crf_version_oid=str(version.get("oid", "")),
        crf_version_name=str(version.get("name", "")),
        primary_form_oid=raw.get("primary_form_oid"),
        primary_form_folder_oid=raw.get("primary_form_folder_oid"),
        default_matrix_oid=raw.get("default_matrix_oid"),
        warnings=list(raw.get("warnings") or []),
    )

    for oid, payload in (raw.get("codelists") or {}).items():
        codelist = CodeList(**_only_known(CodeList, {**payload, "entries": []}))
        codelist.entries = [
            CodeListEntry(**_only_known(CodeListEntry, entry))
            for entry in payload.get("entries") or []
        ]
        model.codelists[oid] = codelist

    for oid, payload in (raw.get("items") or {}).items():
        item = Item(**_only_known(Item, {**payload, "ranges": []}))
        item.ranges = [
            RangeConstraint(**_only_known(RangeConstraint, rc))
            for rc in payload.get("ranges") or []
        ]
        model.items[oid] = item

    for oid, payload in (raw.get("item_groups") or {}).items():
        model.item_groups[oid] = ItemGroup(**_only_known(ItemGroup, payload))

    for oid, payload in (raw.get("forms") or {}).items():
        model.forms[oid] = Form(**_only_known(Form, payload))

    for oid, payload in (raw.get("folders") or {}).items():
        folder = Folder(**_only_known(Folder, {**payload, "forms": []}))
        folder.forms = [
            FormAssignment(**_only_known(FormAssignment, assignment))
            for assignment in payload.get("forms") or []
        ]
        model.folders[oid] = folder

    for oid, payload in (raw.get("matrices") or {}).items():
        model.matrices[oid] = Matrix(**_only_known(Matrix, payload))

    model.measurement_units = dict(raw.get("measurement_units") or {})
    return model
