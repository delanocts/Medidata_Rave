"""ODM assembly (FR-7.1).

Every OID written here comes from the study model or config - nothing is
invented. Two payload shapes are produced:

  AdminData    a Location, to register a site (FR-4.2)
  ClinicalData SubjectData/StudyEventData/FormData/ItemGroupData/ItemData

XML is never hand-written; the rwslib builders own the element shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from rwslib.builders import (
    AdminData,
    ClinicalData,
    FormData,
    ItemData,
    ItemGroupData,
    Location,
    MetaDataVersionRef,
    ODM,
    SiteRef,
    StudyEventData,
    SubjectData,
)
from rwslib.builders.constants import LocationType

from ..model.study_model import StudyModel
from ..utils.logging import get_logger

log = get_logger(__name__)

ORIGINATOR = "rave-testdata-agent"


class OdmBuildError(RuntimeError):
    """Raised when the model cannot support the requested payload."""


def _patch_item_group_oid() -> None:
    """Stop rwslib overwriting every ItemGroupOID with the form's OID.

    rwslib's ItemGroupData.build is:

        def build(self, builder, formname=None):
            params = dict(ItemGroupOID=formname if formname else self.itemgroupoid)

    and FormData.build passes the *form* OID down as `formname`, so each item
    group is emitted with the form's OID rather than its own. That is invisible
    for a form whose only group shares the form's OID, and wrong for every
    `<FORM>_LOG_LINE` group - Rave rejects those with "Field does not exist",
    because the field really is not in the group it was told about.

    Subclassing is not an option: ItemGroupData.__init__ calls
    `super(self.__class__, self).__init__(...)`, which recurses forever in a
    subclass. So the method is patched once, here, at import time.
    """
    original = ItemGroupData.build
    if getattr(original, "_oid_patched", False):
        return

    def build(self, builder, formname=None):  # noqa: ANN001 - matches rwslib
        return original(self, builder, formname=None)

    build._oid_patched = True
    ItemGroupData.build = build


_patch_item_group_oid()


class DateOnlyMetaDataVersionRef(MetaDataVersionRef):
    """MetaDataVersionRef whose EffectiveDate is an ODM `date`, not a datetime.

    rwslib serialises it with `dt_to_iso8601`, producing `2026-08-20T00:00:00`.
    The ODM 1.3 schema types EffectiveDate as xs:date, and Rave rejects the
    datetime form outright:

        The 'EffectiveDate' attribute is invalid ... The Pattern constraint failed.
    """

    def build(self, builder):
        builder.start("MetaDataVersionRef", {
            "StudyOID": self.study_oid,
            "MetaDataVersionOID": self.metadata_version_oid,
            "EffectiveDate": self.effective_date.strftime("%Y-%m-%d"),
        })
        builder.end("MetaDataVersionRef")


class ExactStudyClinicalData(ClinicalData):
    """ClinicalData that emits the StudyOID exactly as Rave reports it.

    rwslib composes the OID as `"%s (%s)" % (project, environment)` - with a
    space - but Rave may address the study as `STUDY(ENV)`, with none.
    The authoritative spelling is whatever ClinicalStudiesRequest returned, so
    it is passed in verbatim rather than re-derived.
    """

    def __init__(self, study_oid: str, metadata_version_oid: str):
        super().__init__(projectname=study_oid, environment="prod",
                         metadata_version_oid=metadata_version_oid)
        self._study_oid = study_oid

    def build(self, builder):
        params = {
            "MetaDataVersionOID": str(self.metadata_version_oid),
            "StudyOID": self._study_oid,
        }
        self.mixin_params(params)
        builder.start("ClinicalData", params)
        for subject in self.subject_data:
            subject.build(builder)
        if self.annotations is not None:
            self.annotations.build(builder)
        builder.end("ClinicalData")


@dataclass
class FormPayload:
    """Values for one form instance, keyed by full ItemDef OID.

    `records` holds repeating item-group rows: group OID -> list of value dicts.
    """
    form_oid: str
    values: dict[str, str] = field(default_factory=dict)
    records: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    form_repeat_key: str | int | None = None


def _tostring(odm: ODM) -> bytes:
    """Serialise an ODM document.

    rwslib builds with the stdlib ElementTree, not lxml, so it must be
    serialised with the same library that produced the elements.
    """
    from xml.etree import ElementTree as ET

    return ET.tostring(odm.getroot(), encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# AdminData - site registration (FR-4.2, OQ-3)
# ---------------------------------------------------------------------------

def build_site_odm(
    study_oid: str,
    site_number: str,
    site_name: str,
    metadata_version_oid: str,
    effective_date: date | datetime | None = None,
) -> bytes:
    """Build the AdminData/Location payload that registers a site.

    Rave accepts AdminData through the clinical-data POST endpoint. Note that
    ODM `Location` carries no country element - country lives under User/Address
    - so `site.country` is recorded in config but cannot be sent here.
    """
    if not site_number or not site_name:
        raise OdmBuildError("site.number and site.name are both required to create a site")

    # NOTE: rwslib inverts this argument -
    #   self.filetype = TRANSACTIONAL if filetype is None else SNAPSHOT
    # so passing FILETYPE_TRANSACTIONAL explicitly yields "Snapshot", which Rave
    # rejects with "Filetype 'Snapshot' not supported". None gives Transactional.
    odm = ODM(originator=ORIGINATOR, filetype=None)
    # Rave's own Sites.odm emits <AdminData> with no StudyOID; the study is
    # referenced from MetaDataVersionRef instead. Match that shape.
    admin = AdminData()
    location = Location(
        oid=site_number,
        name=site_name,
        location_type=LocationType.Site,
    )
    location << DateOnlyMetaDataVersionRef(
        study_oid=study_oid,
        metadata_version_oid=str(metadata_version_oid),
        effective_date=effective_date or date.today(),
    )
    admin << location
    odm << admin
    return _tostring(odm)


# ---------------------------------------------------------------------------
# ClinicalData
# ---------------------------------------------------------------------------

def build_clinical_odm(
    model: StudyModel,
    study_oid: str,
    subject_key: str,
    site_oid: str,
    folder_payloads: dict[str, list[FormPayload]],
    subject_transaction: str = "Update",
    form_transaction: str | None = "Update",
) -> bytes:
    """Assemble a ClinicalData document for one subject.

    `study_oid` must be spelled exactly as Rave reports it, e.g. `STUDY(ENV)`.
    `folder_payloads` maps folder (StudyEvent) OID -> the forms to write there.
    Use `subject_transaction="Insert"` for the very first post that creates the
    subject; Rave rejects a second Insert for the same key.
    """
    # An empty payload set is legitimate: SubjectData/Insert with only a SiteRef
    # is how a subject is created on this instance.

    odm = ODM(originator=ORIGINATOR, filetype=None)   # see note in build_site_odm
    clinical = ExactStudyClinicalData(study_oid, model.crf_version_oid)

    subject = SubjectData(site_oid, subject_key, transaction_type=subject_transaction)

    for folder_oid, payloads in folder_payloads.items():
        if folder_oid not in model.folders:
            raise OdmBuildError(
                f"folder {folder_oid!r} is not in the study model for CRF version "
                f"{model.crf_version_oid}"
            )
        event = StudyEventData(folder_oid, transaction_type=form_transaction)

        for payload in payloads:
            _append_form(model, event, payload, form_transaction)

        subject << event

    clinical << subject
    odm << clinical
    return _tostring(odm)


def _append_form(model: StudyModel, event: StudyEventData,
                 payload: FormPayload, transaction: str | None) -> None:
    form = model.forms.get(payload.form_oid)
    if form is None:
        raise OdmBuildError(f"form {payload.form_oid!r} has no FormDef in the study model")

    form_data = FormData(payload.form_oid, transaction_type=transaction,
                         form_repeat_key=payload.form_repeat_key)

    # Non-repeating values, grouped back into the item group each item belongs to.
    grouped: dict[str, dict[str, str]] = {}
    for item_oid, value in payload.values.items():
        group_oid = _group_for(model, payload.form_oid, item_oid)
        grouped.setdefault(group_oid, {})[item_oid] = value

    for group_oid, values in grouped.items():
        group = ItemGroupData(itemgroupoid=group_oid, transaction_type=transaction)
        for item_oid, value in values.items():
            group << ItemData(item_oid, _render_value(model, item_oid, value))
        form_data << group

    # Repeating groups: one ItemGroupData per log line, addressed by repeat key.
    #
    # "Upsert" is the only transaction type that works for every row. Rave
    # auto-creates a blank first log line, so "Insert" collides with it
    # ("Record already exists"), while "Update" fails for rows beyond it
    # ("Record does not exist"). Upsert with an explicit ItemGroupRepeatKey
    # fills the existing row and creates the rest.
    for group_oid, records in payload.records.items():
        if group_oid not in model.item_groups:
            raise OdmBuildError(
                f"item group {group_oid!r} is not in the study model")
        for index, record in enumerate(records, start=1):
            group = ItemGroupData(itemgroupoid=group_oid, transaction_type="Upsert",
                                  item_group_repeat_key=index)
            for item_oid, value in record.items():
                group << ItemData(item_oid, _render_value(model, item_oid, value))
            form_data << group

    event << form_data


def _group_for(model: StudyModel, form_oid: str, item_oid: str) -> str:
    """Find which item group on this form owns an item."""
    form = model.forms.get(form_oid)
    if form is not None:
        for group_oid in form.item_group_oids:
            group = model.item_groups.get(group_oid)
            if group and item_oid in group.item_oids:
                return group_oid
    raise OdmBuildError(
        f"item {item_oid!r} does not belong to any item group on form {form_oid!r}")


def _render_value(model: StudyModel, item_oid: str, value: Any) -> str:
    """Stringify a value, converting ISO dates into the field's Rave format.

    Generated data carries ISO dates because they are unambiguous; Rave expects
    each field's own mdsol:DateTimeFormat (e.g. `20 AUG 2026`). The conversion
    is deterministic, so it does not count as data generation (FR-6.1).
    """
    from ..generation.validators import format_for_rave

    text = _stringify(value)
    item = model.items.get(item_oid)
    if item is not None and item.is_date_like:
        return format_for_rave(item, text)
    return text


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (date, datetime)):
        return value.strftime("%d %b %Y").upper()
    return str(value)


# ---------------------------------------------------------------------------
# Entry-point resolution (FR-5.3)
# ---------------------------------------------------------------------------

def resolve_entry_point(model: StudyModel) -> tuple[str, str]:
    """Find (folder_oid, form_oid) used to enrol a new subject.

    The form comes from `mdsol:PrimaryFormOID`; the folder is whichever seed
    folder actually carries it. Nothing is hardcoded.
    """
    form_oid = model.primary_form_oid
    if not form_oid:
        raise OdmBuildError(
            "the CRF version declares no mdsol:PrimaryFormOID, so the subject entry "
            "point cannot be determined from metadata"
        )
    if form_oid not in model.forms:
        raise OdmBuildError(
            f"PrimaryFormOID {form_oid!r} has no FormDef in CRF version "
            f"{model.crf_version_oid}")

    folder_oid = model.primary_form_folder_oid
    if folder_oid and folder_oid in model.folders:
        return folder_oid, form_oid

    seed = model.seed_folder_oids
    candidates = [oid for oid in seed if form_oid in model.folders[oid].form_oids]
    if candidates:
        candidates.sort(key=lambda oid: (model.folders[oid].order is None,
                                         model.folders[oid].order))
        return candidates[0], form_oid

    raise OdmBuildError(
        f"cannot determine which folder to file {form_oid!r} under. It is in no seed "
        f"folder ({seed}) and was not observed on any existing subject. Set it "
        "explicitly before creating subjects."
    )
