"""Folder / form assignment from every available source (FR-3.3).

Three sources contribute, in increasing order of authority:

  observed        seen on an existing subject; inferred, not declared
  version         the ODM version metadata - only covers the default matrix
  als             the ALS matrix grids - declares every matrix

Coverage warnings are deliberately deferred to `finalise_assignments`, which
runs after all three have been merged, so the counts describe the finished
model rather than whichever source happened to run first.
"""
from __future__ import annotations

from pathlib import Path

from ..utils.logging import get_logger
from ..utils.xml import parse_xml_file
from .study_model import Folder, FormAssignment, Matrix, StudyModel

log = get_logger(__name__)

ODM = "http://www.cdisc.org/ns/odm/v1.3"
MDSOL = "http://www.mdsol.com/ns/odm/metadata"

_TRUE = {"yes", "true", "1"}


def _flag(value: str | None, default: bool = False) -> bool:
    return default if value is None else value.strip().lower() in _TRUE


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
def apply_version_folders(model: StudyModel, xml_path: Path) -> StudyModel:
    """Merge matrix/folder assignments from VersionFolders.odm for this version."""
    root = parse_xml_file(xml_path)
    version = model.crf_version_oid
    seen_versions: set[str] = set()
    matched = 0

    for mdv in root.findall(f".//{{{ODM}}}MetaDataVersion"):
        mdv_oid = mdv.get("OID") or ""
        seen_versions.add(mdv_oid)
        if mdv_oid != version:
            continue
        matched += 1

        matrix_oid = mdv.get(f"{{{MDSOL}}}MatrixOID")
        is_default = matrix_oid is None
        # Rave leaves MatrixOID off the default matrix's blocks.
        key = matrix_oid or (model.default_matrix_oid or "DEFAULT")

        matrix = model.matrices.get(key) or Matrix(oid=key, is_default=is_default)
        matrix.is_default = matrix.is_default or is_default

        for ref in mdv.findall(f".//{{{ODM}}}StudyEventRef"):
            folder_oid = ref.get("StudyEventOID")
            if not folder_oid:
                continue
            if folder_oid not in matrix.folder_oids:
                matrix.folder_oids.append(folder_oid)

            # Folders reachable only through a non-default matrix have no
            # StudyEventDef in the version metadata; synthesise them so the
            # dynamics loop can still name them.
            existing = model.folders.get(folder_oid)
            if existing is None:
                model.folders[folder_oid] = Folder(
                    oid=folder_oid,
                    name=ref.get(f"{{{MDSOL}}}StudyEventDefName") or folder_oid,
                    event_type=ref.get(f"{{{MDSOL}}}StudyEventDefType") or "Common",
                    repeating=_flag(ref.get(f"{{{MDSOL}}}StudyEventDefRepeating")),
                    order=_int(ref.get("OrderNumber")),
                )
            elif existing.order is None:
                existing.order = _int(ref.get("OrderNumber"))

        model.matrices[key] = matrix

    if not matched:
        model.warnings.append(
            f"VersionFolders.odm contains no MetaDataVersion for CRF version {version} "
            f"(saw {sorted(seen_versions)}); matrices could not be resolved"
        )
        log.warning("no matrix blocks for version", extra={"version": version})

    return model


# ---------------------------------------------------------------------------
def apply_observed_structure(model: StudyModel, observed) -> StudyModel:
    """Fill folder/form assignments learned by sampling existing subjects.

    Only folders left empty by the metadata are populated, and each addition is
    marked `source="observed"` so an inferred assignment stays distinguishable
    from a declared one. Declared assignments are never overwritten.
    """
    unknown_forms: set[str] = set()

    for folder_oid, forms in (observed.folder_forms or {}).items():
        folder = model.folders.get(folder_oid)
        if folder is None:
            folder = Folder(oid=folder_oid, name=folder_oid)
            model.folders[folder_oid] = folder

        if folder.forms:
            continue  # already declared by the metadata

        for index, form_oid in enumerate(sorted(forms)):
            if form_oid not in model.forms:
                unknown_forms.add(form_oid)
                continue
            folder.forms.append(FormAssignment(
                form_oid=form_oid, mandatory=False, order=index, source="observed"))

    if unknown_forms:
        model.warnings.append(
            f"{len(unknown_forms)} observed form(s) have no FormDef in this CRF version "
            f"and were skipped: {sorted(unknown_forms)[:10]}"
        )
    return model


# ---------------------------------------------------------------------------
def apply_als_matrices(model: StudyModel, als) -> StudyModel:
    """Apply folder/form assignments declared by the ALS matrix grids.

    The ALS is authoritative: it declares assignments for every matrix, not just
    the default one, so it supersedes anything inferred by observing subjects.
    """
    added_folders = 0
    added_assignments = 0
    unknown_forms: set[str] = set()

    for matrix_oid, grid in (als.matrices or {}).items():
        matrix = model.matrices.get(matrix_oid)
        if matrix is None:
            matrix = Matrix(oid=matrix_oid,
                            is_default=(matrix_oid == model.default_matrix_oid))
            model.matrices[matrix_oid] = matrix

        for folder_oid, form_oids in grid.items():
            if folder_oid not in matrix.folder_oids:
                matrix.folder_oids.append(folder_oid)

            folder = model.folders.get(folder_oid)
            if folder is None:
                folder = Folder(oid=folder_oid,
                                name=(als.folder_names or {}).get(folder_oid, folder_oid))
                model.folders[folder_oid] = folder
                added_folders += 1

            existing = {a.form_oid: a for a in folder.forms}
            for index, form_oid in enumerate(form_oids):
                if form_oid not in model.forms:
                    unknown_forms.add(form_oid)
                    continue
                current = existing.get(form_oid)
                if current is None:
                    folder.forms.append(FormAssignment(
                        form_oid=form_oid, mandatory=False, order=index, source="als",
                        matrix_oid="" if matrix.is_default else matrix_oid))
                    added_assignments += 1
                elif current.source == "observed":
                    # A declared assignment supersedes an inferred one.
                    current.source = "als"
                    current.matrix_oid = "" if matrix.is_default else matrix_oid
                elif matrix.is_default:
                    # The seed matrix always wins: the form is there from the start.
                    current.matrix_oid = ""

    if added_assignments:
        model.warnings.append(
            f"ALS declared {added_assignments} form assignment(s) across "
            f"{len(als.matrices)} matrix/matrices"
            + (f", adding {added_folders} folder(s) absent from the version metadata"
               if added_folders else "")
        )
    if unknown_forms:
        model.warnings.append(
            f"{len(unknown_forms)} form(s) in ALS matrices have no FormDef in CRF version "
            f"{model.crf_version_oid} and were skipped: {sorted(unknown_forms)[:10]}"
        )
    return model


# ---------------------------------------------------------------------------
def finalise_assignments(model: StudyModel) -> StudyModel:
    """Report assignment coverage once every source has contributed."""
    by_source: dict[str, int] = {}
    for folder in model.folders.values():
        for assignment in folder.forms:
            by_source[assignment.source] = by_source.get(assignment.source, 0) + 1

    if by_source:
        rendered = ", ".join(f"{count} {source}" for source, count in sorted(by_source.items()))
        model.warnings.append(f"Form assignments by source: {rendered}.")

    observed = by_source.get("observed")
    if observed:
        model.warnings.append(
            f"{observed} assignment(s) rest only on observing existing subjects and are "
            "not declared by the ALS or the version metadata."
        )

    empty = sorted(f.oid for f in model.folders.values() if not f.forms)
    if empty:
        model.warnings.append(
            f"{len(empty)} folder(s) have no form assignments from any source and cannot "
            f"be populated: {empty}"
        )

    orphans = model.unassigned_forms()
    if orphans:
        model.warnings.append(
            f"{len(orphans)} form(s) are assigned to no folder in any matrix: {orphans[:15]}"
            + (" ..." if len(orphans) > 15 else "")
        )
    return model


# ---------------------------------------------------------------------------
def resolve_primary_form_placement(model: StudyModel, observed=None) -> StudyModel:
    """Work out which folder Rave files the subject-entry form under (FR-5.3).

    Rave auto-places the `mdsol:PrimaryFormOID` form when a subject is created,
    so it is often absent from every matrix grid. Observing where it actually
    landed for existing subjects is more reliable than any declaration; a seed
    folder is preferred, and the most frequently seen placement wins.
    """
    form_oid = model.primary_form_oid
    if not form_oid:
        return model

    seed = set(model.seed_folder_oids)

    if observed is not None:
        placements = {
            folder_oid: forms[form_oid]
            for folder_oid, forms in (observed.folder_forms or {}).items()
            if form_oid in forms
        }
        if placements:
            ranked = sorted(
                placements.items(),
                key=lambda kv: (kv[0] not in seed, -kv[1], kv[0]),
            )
            model.primary_form_folder_oid = ranked[0][0]
            if len(ranked) > 1:
                model.warnings.append(
                    f"entry-point form {form_oid} was observed in "
                    f"{ {k: v for k, v in placements.items()} }; chose "
                    f"{model.primary_form_folder_oid}"
                )
            return model

    declared = [oid for oid in seed if form_oid in model.folders[oid].form_oids]
    if declared:
        declared.sort(key=lambda oid: (model.folders[oid].order is None,
                                       model.folders[oid].order))
        model.primary_form_folder_oid = declared[0]
        return model

    model.warnings.append(
        f"entry-point form {form_oid} could not be placed in a seed folder from "
        "either the matrices or subject observations; subject creation will need "
        "an explicit folder"
    )
    return model


# ---------------------------------------------------------------------------
def summarise(model: StudyModel) -> dict:
    """A compact view of seed vs dynamic reach, for reporting."""
    seed = set(model.seed_folder_oids)
    reachable = {oid for m in model.matrices.values() for oid in m.folder_oids}
    return {
        "default_matrix": model.default_matrix_oid,
        "seed_folders": sorted(seed),
        "seed_folder_count": len(seed),
        "matrix_count": len(model.matrices),
        "reachable_folder_count": len(reachable),
        "beyond_seed": sorted(reachable - seed),
        "matrices": {
            m.oid: {"is_default": m.is_default, "folders": m.folder_oids}
            for m in sorted(model.matrices.values(), key=lambda x: (not x.is_default, x.oid))
        },
    }


def apply_als_derivations(model: StudyModel, als) -> StudyModel:
    """Flag fields Rave derives, so generation never tries to write them.

    Posting a value to a derived field is refused outright:
    "Transaction on derived field is not permitted."
    """
    marked, unknown = 0, []
    for derivation in als.derivations or []:
        if not derivation.get("active", True):
            continue
        form_oid = (derivation.get("form_oid") or "").strip()
        field_oid = (derivation.get("field_oid") or "").strip()
        variable_oid = (derivation.get("variable_oid") or "").strip()

        targets = []
        if form_oid and field_oid:
            item = model.items.get(f"{form_oid}.{field_oid}")
            if item is not None:
                targets = [item]
        elif field_oid or variable_oid:
            # Only the variable is named, so match it across every form.
            name = field_oid or variable_oid
            targets = [i for i in model.items.values() if i.name == name]

        if not targets:
            label = f"{form_oid}.{field_oid or variable_oid}".strip(".")
            if label:
                unknown.append(label)
            continue

        for item in targets:
            if not item.derived:
                item.derived = True
                marked += 1

    if marked:
        model.warnings.append(
            f"{marked} field(s) are derived by Rave and will not be generated or posted.")
    if unknown:
        model.warnings.append(
            f"{len(unknown)} derivation target(s) have no ItemDef in this CRF version: "
            f"{sorted(unknown)[:10]}")
    return model
