"""The normalized study model - the contract A5/A6/A8 consume (FR-3.7).

Deliberately plain dataclasses with a stable, human-readable JSON shape. No
study-specific knowledge lives here; everything is populated from metadata.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CodeListEntry:
    coded_value: str
    decode: str = ""
    order: int | None = None
    specify: bool = False


@dataclass
class CodeList:
    oid: str
    name: str
    data_type: str
    entries: list[CodeListEntry] = field(default_factory=list)

    @property
    def coded_values(self) -> list[str]:
        return [e.coded_value for e in self.entries]


@dataclass
class RangeConstraint:
    comparator: str          # LT, LE, GT, GE, EQ, NE, BETWEEN...
    values: list[str] = field(default_factory=list)
    soft_hard: str = "Soft"  # a Soft breach raises a query; Hard blocks entry


@dataclass
class Item:
    """One field on a form."""
    oid: str                       # e.g. VS_F.VSORRES_T_SYSBP
    name: str                      # the variable name
    form_oid: str
    data_type: str                 # text, integer, float, date, datetime, time
    label: str = ""                # the question text, English
    length: int | None = None
    significant_digits: int | None = None
    control_type: str = ""         # Text, RadioButton, DropDownList, CheckBox...
    datetime_format: str = ""      # e.g. dd MMM yyyy
    codelist_oid: str | None = None
    measurement_unit: str | None = None
    mandatory: bool = False
    visible: bool = True           # mdsol:Visible=No means hidden until activated
    active: bool = True
    default_value: str | None = None
    order: int | None = None
    ranges: list[RangeConstraint] = field(default_factory=list)
    entry_restrictions: list[str] = field(default_factory=list)
    derived: bool = False          # Rave computes it; posting one is rejected
    query_non_conformance: bool = False
    query_future_date: bool = False
    source_document: bool = False

    @property
    def is_coded(self) -> bool:
        return self.codelist_oid is not None

    @property
    def is_date_like(self) -> bool:
        return self.data_type in ("date", "datetime", "time")


@dataclass
class ItemGroup:
    """A section of a form. Repeating groups are log lines."""
    oid: str
    name: str
    repeating: bool = False
    mandatory: bool = False
    item_oids: list[str] = field(default_factory=list)


@dataclass
class Form:
    oid: str
    name: str
    repeating: bool = False
    order: int | None = None
    signature_required: bool = False
    log_direction: str = ""
    double_data_entry: bool = False
    item_group_oids: list[str] = field(default_factory=list)
    # NOTE: FormDef@Repeating is not a reliable log-form signal in Rave - studies
    # commonly set it on every form. Log lines are repeating *item groups*, so
    # use StudyModel.log_item_groups() rather than this flag.


@dataclass
class FormAssignment:
    form_oid: str
    mandatory: bool = False
    order: int | None = None
    source: str = "metadata"   # metadata = declared in ODM; observed = seen on a real subject
    matrix_oid: str = ""       # which matrix declares it; "" = the default/seed matrix


@dataclass
class Folder:
    """A study event / visit."""
    oid: str
    name: str
    event_type: str = "Common"     # Common, Scheduled, Unscheduled
    repeating: bool = False
    order: int | None = None
    target_days: int | None = None
    start_win_days: int | None = None
    end_win_days: int | None = None
    forms: list[FormAssignment] = field(default_factory=list)

    @property
    def form_oids(self) -> list[str]:
        return [f.form_oid for f in self.forms]


@dataclass
class Matrix:
    """A named set of folder assignments. The default matrix is the seed set."""
    oid: str
    is_default: bool = False
    folder_oids: list[str] = field(default_factory=list)


@dataclass
class StudyModel:
    study_name: str
    environment: str
    crf_version_oid: str
    crf_version_name: str
    primary_form_oid: str | None = None      # subject entry point form (FR-5.3)
    primary_form_folder_oid: str | None = None   # folder Rave actually files it under
    default_matrix_oid: str | None = None

    folders: dict[str, Folder] = field(default_factory=dict)
    forms: dict[str, Form] = field(default_factory=dict)
    item_groups: dict[str, ItemGroup] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    codelists: dict[str, CodeList] = field(default_factory=dict)
    matrices: dict[str, Matrix] = field(default_factory=dict)
    measurement_units: dict[str, str] = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)

    # -- derived views used downstream ---------------------------------
    @property
    def seed_folder_oids(self) -> list[str]:
        """Folders a fresh subject sees, from the default matrix (FR-3.3)."""
        default = next((m for m in self.matrices.values() if m.is_default), None)
        if default and default.folder_oids:
            return list(default.folder_oids)
        return list(self.folders.keys())

    def in_schedule_order(self, folder_oids: list[str]) -> list[str]:
        """Folder OIDs in visit order rather than alphabetical order.

        `Folder.order` is the StudyEventRef ordinal - the sequence the study
        runs in. Sorting by OID instead puts `D420` (the final visit) before
        `RAND` (Day 1) and `FU_D91` after `FU_D331`, because those are string
        comparisons. Generation carries each visit's date forward into the next
        visit's prompt, so out-of-sequence generation asks for a date with no
        earlier visit to measure from.

        Folders without an ordinal keep a deterministic alphabetical tail, so
        the order is stable whether or not the study publishes one.
        """
        def key(oid: str) -> tuple:
            folder = self.folders.get(oid)
            order = folder.order if folder else None
            return (order is None, order if order is not None else 0, oid)

        return sorted(folder_oids, key=key)

    @property
    def non_default_matrices(self) -> list[Matrix]:
        return [m for m in self.matrices.values() if not m.is_default]

    def items_for_form(self, form_oid: str) -> list[Item]:
        return [i for i in self.items.values() if i.form_oid == form_oid]

    def items_for_group(self, group_oid: str) -> list[Item]:
        group = self.item_groups.get(group_oid)
        if not group:
            return []
        return [self.items[oid] for oid in group.item_oids if oid in self.items]

    def forms_in_folder(self, folder_oid: str) -> list[Form]:
        folder = self.folders.get(folder_oid)
        if not folder:
            return []
        return [self.forms[oid] for oid in folder.form_oids if oid in self.forms]

    def log_item_groups(self, form_oid: str) -> list[ItemGroup]:
        """Repeating sections of a form - these get N generated records (FR-6.7)."""
        form = self.forms.get(form_oid)
        if not form:
            return []
        return [self.item_groups[oid] for oid in form.item_group_oids
                if oid in self.item_groups and self.item_groups[oid].repeating]

    def forms_with_log_sections(self) -> list[str]:
        return sorted(oid for oid in self.forms if self.log_item_groups(oid))

    def hidden_items(self) -> list[Item]:
        """Fields flagged not-visible - candidates for dynamic activation."""
        return [i for i in self.items.values() if not i.visible]

    def unassigned_forms(self) -> list[str]:
        """Forms defined but not in any folder of any matrix."""
        assigned = {oid for f in self.folders.values() for oid in f.form_oids}
        return sorted(set(self.forms) - assigned)

    def stats(self) -> dict[str, Any]:
        return {
            "folders": len(self.folders),
            "forms": len(self.forms),
            "item_groups": len(self.item_groups),
            "items": len(self.items),
            "codelists": len(self.codelists),
            "matrices": len(self.matrices),
            "repeating_item_groups": sum(1 for g in self.item_groups.values() if g.repeating),
            "forms_with_log_sections": len(self.forms_with_log_sections()),
            "coded_items": sum(1 for i in self.items.values() if i.is_coded),
            "date_items": sum(1 for i in self.items.values() if i.is_date_like),
            "mandatory_items": sum(1 for i in self.items.values() if i.mandatory),
            "hidden_items": len(self.hidden_items()),
            "derived_items": sum(1 for i in self.items.values() if i.derived),
            "items_with_ranges": sum(1 for i in self.items.values() if i.ranges),
            "seed_folders": len(self.seed_folder_oids),
            "unassigned_forms": len(self.unassigned_forms()),
            "folders_with_forms": sum(1 for f in self.folders.values() if f.forms),
            "observed_assignments": sum(
                1 for f in self.folders.values() for a in f.forms if a.source == "observed"),
        }

    # -- persistence ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "study_name": self.study_name,
            "environment": self.environment,
            "crf_version": {"oid": self.crf_version_oid, "name": self.crf_version_name},
            "primary_form_oid": self.primary_form_oid,
            "primary_form_folder_oid": self.primary_form_folder_oid,
            "default_matrix_oid": self.default_matrix_oid,
            "seed_folder_oids": self.seed_folder_oids,
            "stats": self.stats(),
            "warnings": self.warnings,
            "folders": {k: asdict(v) for k, v in self.folders.items()},
            "forms": {k: asdict(v) for k, v in self.forms.items()},
            "item_groups": {k: asdict(v) for k, v in self.item_groups.items()},
            "items": {k: asdict(v) for k, v in self.items.items()},
            "codelists": {k: asdict(v) for k, v in self.codelists.items()},
            "matrices": {k: asdict(v) for k, v in self.matrices.items()},
            "measurement_units": self.measurement_units,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path
