"""ODM metadata -> normalized study model (FR-3.1).

The ODM for the active CRF version is the source of truth for structure. No
form, field or study identifier is hardcoded.
"""
from __future__ import annotations

from pathlib import Path

from lxml import etree

from ..utils.logging import get_logger
from ..utils.xml import parse_xml_file
from .study_model import (
    CodeList,
    CodeListEntry,
    Folder,
    Form,
    FormAssignment,
    Item,
    ItemGroup,
    RangeConstraint,
    StudyModel,
)

log = get_logger(__name__)

ODM = "http://www.cdisc.org/ns/odm/v1.3"
MDSOL = "http://www.mdsol.com/ns/odm/metadata"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

_TRUE = {"yes", "true", "1"}


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUE


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mdsol(element, name: str) -> str | None:
    return element.get(f"{{{MDSOL}}}{name}")


def _translated(parent, tag: str, lang: str = "en") -> str:
    """Pick the requested language from a TranslatedText set, else the first."""
    if parent is None:
        return ""
    nodes = parent.findall(f".//{{{ODM}}}{tag}")
    if not nodes:
        return ""
    for node in nodes:
        if (node.get(XML_LANG) or "").lower().startswith(lang):
            return (node.text or "").strip()
    return (nodes[0].text or "").strip()


class OdmParser:
    def __init__(self, xml_path: Path):
        self.path = xml_path
        self.root = parse_xml_file(xml_path)
        self.warnings: list[str] = []

    # ------------------------------------------------------------------
    def parse(self, study_name: str, environment: str) -> StudyModel:
        mdv = self.root.find(f".//{{{ODM}}}MetaDataVersion")
        if mdv is None:
            raise ValueError(f"no MetaDataVersion element in {self.path}")

        model = StudyModel(
            study_name=study_name,
            environment=environment,
            crf_version_oid=mdv.get("OID") or "",
            crf_version_name=mdv.get("Name") or "",
            primary_form_oid=_mdsol(mdv, "PrimaryFormOID"),
            default_matrix_oid=_mdsol(mdv, "DefaultMatrixOID"),
        )

        self._parse_measurement_units(model)
        self._parse_codelists(model)
        self._parse_items(model)
        self._parse_item_groups(model)
        self._parse_forms(model)
        self._parse_folders(model)

        self._validate(model)
        model.warnings.extend(self.warnings)
        return model

    # ------------------------------------------------------------------
    def _parse_measurement_units(self, model: StudyModel) -> None:
        for unit in self.root.findall(f".//{{{ODM}}}MeasurementUnit"):
            oid = unit.get("OID")
            if oid:
                model.measurement_units[oid] = _translated(unit, "TranslatedText") or oid

    def _parse_codelists(self, model: StudyModel) -> None:
        for node in self.root.findall(f".//{{{ODM}}}CodeList"):
            oid = node.get("OID")
            if not oid:
                continue
            codelist = CodeList(
                oid=oid,
                name=node.get("Name") or oid,
                data_type=node.get("DataType") or "text",
            )
            for entry in node.findall(f"{{{ODM}}}CodeListItem"):
                codelist.entries.append(CodeListEntry(
                    coded_value=entry.get("CodedValue") or "",
                    decode=_translated(entry.find(f"{{{ODM}}}Decode"), "TranslatedText"),
                    order=_int(_mdsol(entry, "OrderNumber")),
                    specify=_flag(_mdsol(entry, "Specify")),
                ))
            codelist.entries.sort(key=lambda e: (e.order is None, e.order))
            model.codelists[oid] = codelist

    def _parse_items(self, model: StudyModel) -> None:
        for node in self.root.findall(f".//{{{ODM}}}ItemDef"):
            oid = node.get("OID")
            if not oid:
                continue
            # Rave item OIDs are FORM_OID.VARIABLE_OID
            form_oid = oid.split(".", 1)[0] if "." in oid else ""

            codelist_ref = node.find(f"{{{ODM}}}CodeListRef")
            unit_ref = node.find(f"{{{ODM}}}MeasurementUnitRef")

            ranges = []
            for rc in node.findall(f"{{{ODM}}}RangeCheck"):
                ranges.append(RangeConstraint(
                    comparator=rc.get("Comparator") or "",
                    values=[(cv.text or "").strip()
                            for cv in rc.findall(f"{{{ODM}}}CheckValue")],
                    soft_hard=rc.get("SoftHard") or "Soft",
                ))

            model.items[oid] = Item(
                oid=oid,
                name=node.get("Name") or _mdsol(node, "VariableOID") or oid,
                form_oid=form_oid,
                data_type=(node.get("DataType") or "text").lower(),
                label=_translated(node.find(f"{{{ODM}}}Question"), "TranslatedText"),
                length=_int(node.get("Length")),
                significant_digits=_int(node.get("SignificantDigits")),
                control_type=_mdsol(node, "ControlType") or "",
                datetime_format=_mdsol(node, "DateTimeFormat") or "",
                codelist_oid=codelist_ref.get("CodeListOID") if codelist_ref is not None else None,
                measurement_unit=(unit_ref.get("MeasurementUnitOID")
                                  if unit_ref is not None else None),
                visible=_flag(_mdsol(node, "Visible"), default=True),
                active=_flag(_mdsol(node, "Active"), default=True),
                default_value=_mdsol(node, "DefaultValue"),
                ranges=ranges,
                entry_restrictions=[(e.text or "").strip()
                                    for e in node.findall(f"{{{MDSOL}}}EntryRestriction")],
                query_non_conformance=_flag(_mdsol(node, "QueryNonConformance")),
                query_future_date=_flag(_mdsol(node, "QueryFutureDate")),
                source_document=_flag(_mdsol(node, "SourceDocument")),
            )

    def _parse_item_groups(self, model: StudyModel) -> None:
        for node in self.root.findall(f".//{{{ODM}}}ItemGroupDef"):
            oid = node.get("OID")
            if not oid:
                continue
            group = ItemGroup(
                oid=oid,
                name=node.get("Name") or oid,
                repeating=_flag(node.get("Repeating")),
            )
            refs = []
            for ref in node.findall(f"{{{ODM}}}ItemRef"):
                item_oid = ref.get("ItemOID")
                if not item_oid:
                    continue
                refs.append((_int(ref.get("OrderNumber")), item_oid))
                item = model.items.get(item_oid)
                if item is not None:
                    item.mandatory = _flag(ref.get("Mandatory"))
                    item.order = _int(ref.get("OrderNumber"))
                else:
                    self.warnings.append(
                        f"ItemGroupDef {oid} references unknown ItemOID {item_oid}")
            refs.sort(key=lambda r: (r[0] is None, r[0]))
            group.item_oids = [oid_ for _, oid_ in refs]
            model.item_groups[oid] = group

    def _parse_forms(self, model: StudyModel) -> None:
        for node in self.root.findall(f".//{{{ODM}}}FormDef"):
            oid = node.get("OID")
            if not oid:
                continue
            form = Form(
                oid=oid,
                name=node.get("Name") or oid,
                repeating=_flag(node.get("Repeating")),
                order=_int(_mdsol(node, "OrderNumber")),
                signature_required=_flag(_mdsol(node, "SignatureRequired")),
                log_direction=_mdsol(node, "LogDirection") or "",
                double_data_entry=_flag(_mdsol(node, "DoubleDataEntry")),
            )
            for ref in node.findall(f"{{{ODM}}}ItemGroupRef"):
                group_oid = ref.get("ItemGroupOID")
                if not group_oid:
                    continue
                form.item_group_oids.append(group_oid)
                group = model.item_groups.get(group_oid)
                if group is not None:
                    group.mandatory = _flag(ref.get("Mandatory"))
                else:
                    self.warnings.append(
                        f"FormDef {oid} references unknown ItemGroupOID {group_oid}")
            model.forms[oid] = form

    def _parse_folders(self, model: StudyModel) -> None:
        for node in self.root.findall(f".//{{{ODM}}}StudyEventDef"):
            oid = node.get("OID")
            if not oid:
                continue
            folder = Folder(
                oid=oid,
                name=node.get("Name") or oid,
                event_type=node.get("Type") or "Common",
                repeating=_flag(node.get("Repeating")),
                order=_int(_mdsol(node, "OrderNumber")),
                target_days=_int(_mdsol(node, "TargetDays")),
                start_win_days=_int(_mdsol(node, "StartWinDays")),
                end_win_days=_int(_mdsol(node, "EndWinDays")),
            )
            for index, ref in enumerate(node.findall(f"{{{ODM}}}FormRef")):
                form_oid = ref.get("FormOID")
                if not form_oid:
                    continue
                folder.forms.append(FormAssignment(
                    form_oid=form_oid,
                    mandatory=_flag(ref.get("Mandatory")),
                    order=_int(ref.get("OrderNumber")) or index,
                ))
            model.folders[oid] = folder

    # ------------------------------------------------------------------
    def _validate(self, model: StudyModel) -> None:
        """Record referential problems rather than dropping them silently."""
        for item in model.items.values():
            if item.codelist_oid and item.codelist_oid not in model.codelists:
                self.warnings.append(
                    f"Item {item.oid} references unknown CodeList {item.codelist_oid}")
            if item.measurement_unit and item.measurement_unit not in model.measurement_units:
                self.warnings.append(
                    f"Item {item.oid} references unknown MeasurementUnit {item.measurement_unit}")
            if item.form_oid and item.form_oid not in model.forms:
                self.warnings.append(
                    f"Item {item.oid} implies form {item.form_oid} which has no FormDef")

        for folder in model.folders.values():
            for assignment in folder.forms:
                if assignment.form_oid not in model.forms:
                    self.warnings.append(
                        f"Folder {folder.oid} assigns unknown form {assignment.form_oid}")

        if model.primary_form_oid and model.primary_form_oid not in model.forms:
            self.warnings.append(
                f"PrimaryFormOID {model.primary_form_oid} has no FormDef in this version")


def parse_odm(xml_path: Path, study_name: str, environment: str) -> StudyModel:
    return OdmParser(xml_path).parse(study_name, environment)
