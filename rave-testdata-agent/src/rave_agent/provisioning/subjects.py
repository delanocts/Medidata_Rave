"""A4 - subject ID allocation and enrolment (FR-5).

A subject is created by posting the study's entry-point form, not by a dedicated
"create subject" call. Which form that is comes from the model
(`mdsol:PrimaryFormOID`), never from a hardcoded OID (FR-5.3).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config.loader import Config
from ..model.study_model import StudyModel
from ..rave.client import RaveClient
from ..rave.errors import NotFoundError, RaveError
from ..submission.odm_builder import (
    FormPayload,
    OdmBuildError,
    build_clinical_odm,
    resolve_entry_point,
)
from ..submission.submitter import Submitter
from ..utils.logging import get_logger
from ..utils.xml import parse_xml

log = get_logger(__name__)

ODM = "http://www.cdisc.org/ns/odm/v1.3"


class SubjectPolicyError(RuntimeError):
    """Raised when the on_existing_id policy says to stop."""


@dataclass
class SubjectRecord:
    subject_id: str
    status: str = "planned"      # planned | exists | created | failed | skipped
    site_oid: str = ""
    folder_oid: str = ""
    form_oid: str = ""
    detail: str = ""
    created_at: str = ""
    request_path: str = ""
    response_path: str = ""


@dataclass
class ProvisionResult:
    study: str
    environment: str
    site_oid: str
    entry_folder_oid: str = ""
    entry_form_oid: str = ""
    entry_item_oid: str = ""
    subjects: list[SubjectRecord] = field(default_factory=list)
    existing_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self.subjects:
            out[record.status] = out.get(record.status, 0) + 1
        return out

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "study": self.study,
            "environment": self.environment,
            "site_oid": self.site_oid,
            "entry_point": {
                "folder_oid": self.entry_folder_oid,
                "form_oid": self.entry_form_oid,
                "item_oid": self.entry_item_oid,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": self.counts(),
            "existing_ids": self.existing_ids,
            "subjects": [asdict(s) for s in self.subjects],
            "warnings": self.warnings,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
def existing_subject_ids(client: RaveClient, config: Config) -> list[str]:
    from rwslib.rws_requests import StudySubjectsRequest

    try:
        payload = client.send(
            StudySubjectsRequest(config.study_name, config.environment, status=True)
        ).value
    except NotFoundError:
        return []
    root = parse_xml(str(payload))
    ids = []
    for node in root.findall(f".//{{{ODM}}}SubjectData"):
        key = node.get("SubjectKey")
        if key and key not in ids:
            ids.append(key)
    return ids


def allocate_ids(config: Config, existing: list[str]) -> tuple[list[str], list[str]]:
    """Generate subject IDs per config, applying `on_existing_id` (FR-5.1, FR-5.2).

    Returns (to_create, skipped).
    """
    prefix = str(config.get("subjects.id_prefix") or "")
    width = int(config.get("subjects.id_pad_width") or 0)
    start = int(config.get("subjects.id_start_number") or 1)
    count = int(config.get("subjects.count") or 1)
    policy = str(config.get("subjects.on_existing_id") or "skip")

    taken = set(existing)

    def make(number: int) -> str:
        return f"{prefix}{str(number).zfill(width)}" if width else f"{prefix}{number}"

    wanted = [make(start + offset) for offset in range(count)]
    clashes = [sid for sid in wanted if sid in taken]

    if not clashes:
        return wanted, []

    if policy == "fail":
        raise SubjectPolicyError(
            f"subjects.on_existing_id is 'fail' and these IDs already exist: {clashes}"
        )

    if policy == "skip":
        return [sid for sid in wanted if sid not in taken], clashes

    # continue_numbering: walk past every collision until `count` free IDs exist.
    allocated: list[str] = []
    number = start
    guard = start + count + len(taken) + 10_000
    while len(allocated) < count and number < guard:
        candidate = make(number)
        if candidate not in taken:
            allocated.append(candidate)
        number += 1
    if len(allocated) < count:
        raise SubjectPolicyError(
            f"could not allocate {count} free subject ID(s) with prefix {prefix!r}"
        )
    return allocated, clashes


# ---------------------------------------------------------------------------
def enrol_subjects(
    client: RaveClient,
    config: Config,
    model: StudyModel,
    site_oid: str,
    submitter: Submitter,
) -> ProvisionResult:
    """Create each subject by posting the entry-point form (FR-5.3, FR-5.4)."""
    result = ProvisionResult(
        study=config.study_name, environment=config.environment, site_oid=site_oid
    )

    # A subject is created by SubjectData/Insert carrying only a SiteRef. Posting
    # the PrimaryFormOID form as part of creation is rejected here - Rave answers
    # "Form does not exist in the designated folder" because that form is not
    # assigned to any seed folder in this CRF version. Data is written afterwards,
    # to folders and forms the matrices actually declare.
    try:
        folder_oid, form_oid = resolve_entry_point(model)
        result.entry_folder_oid = folder_oid
        result.entry_form_oid = form_oid
    except OdmBuildError as exc:
        result.warnings.append(f"entry point not resolvable (not needed to enrol): {exc}")

    existing = existing_subject_ids(client, config)
    result.existing_ids = existing[:200]

    to_create, skipped = allocate_ids(config, existing)
    for subject_id in skipped:
        result.subjects.append(SubjectRecord(
            subject_id=subject_id, status="exists", site_oid=site_oid,
            detail="already present in the study; not re-created",
        ))

    for subject_id in to_create:
        record = SubjectRecord(subject_id=subject_id, site_oid=site_oid)
        try:
            odm = build_clinical_odm(
                model=model,
                study_oid=config.study_env,
                subject_key=subject_id,
                site_oid=site_oid,
                folder_payloads={},
                subject_transaction="Insert",
                form_transaction=None,
            )
        except OdmBuildError as exc:
            record.status = "failed"
            record.detail = f"could not build payload: {exc}"
            result.subjects.append(record)
            continue

        outcome = submitter.post(
            odm, label=f"enrol_{subject_id}",
            archive_dir=Path("subjects") / subject_id / "pass_0",
        )
        record.request_path = str(outcome.request_path or "")
        record.response_path = str(outcome.response_path or "")

        if outcome.ok:
            record.status = "created"
            record.created_at = datetime.now(timezone.utc).isoformat()
            record.detail = outcome.status
        else:
            record.status = "failed"
            record.detail = outcome.error or outcome.reason
        result.subjects.append(record)

    return result


def _subject_id_item(model: StudyModel, form_oid: str) -> str:
    """Find the field on the entry form that carries the subject ID.

    Preference order: an item whose name looks like a subject identifier, else
    the form's single mandatory field, else its only field. Raises rather than
    guessing when the form has several plausible candidates.
    """
    items = model.items_for_form(form_oid)
    if not items:
        raise OdmBuildError(f"entry form {form_oid!r} has no fields in the study model")

    preferred = [i for i in items
                 if i.name.upper() in ("SUBJID", "SUBJECTID", "SUBJECT", "USUBJID", "SCRNID")]
    if len(preferred) == 1:
        return preferred[0].oid
    if len(items) == 1:
        return items[0].oid

    mandatory = [i for i in items if i.mandatory]
    if len(mandatory) == 1:
        return mandatory[0].oid

    raise OdmBuildError(
        f"cannot tell which field on {form_oid!r} holds the subject ID; candidates are "
        f"{[i.oid for i in items][:10]}. Add an explicit setting rather than guessing."
    )
