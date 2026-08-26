"""A4 - site verification and creation (FR-4).

Creation posts an AdminData/Location payload. If the account lacks the right,
that is reported as a clear instruction to create the site manually rather than
being retried or swallowed (FR-4.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config.loader import Config
from ..metadata.downloader import site_version_refs
from ..rave.client import RaveClient
from ..rave.errors import RaveError
from ..submission.odm_builder import build_site_odm
from ..submission.submitter import Submitter
from ..utils.logging import get_logger
from ..utils.xml import parse_xml

log = get_logger(__name__)

ODM = "http://www.cdisc.org/ns/odm/v1.3"


@dataclass
class SiteState:
    number: str
    name: str
    exists: bool = False
    created: bool = False
    action: str = ""          # exists | created | create_failed | missing
    detail: str = ""
    known_sites: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "number": self.number, "name": self.name, "exists": self.exists,
            "created": self.created, "action": self.action, "detail": self.detail,
            "known_sites": self.known_sites,
        }


def list_sites(client: RaveClient, config: Config) -> list[dict]:
    """Every Location currently registered for the study/environment."""
    from rwslib.rws_requests.odm_adapter import SitesMetadataRequest

    payload = client.send(
        SitesMetadataRequest(config.study_name, config.environment)
    ).value
    root = parse_xml(payload)
    return [
        {"oid": loc.get("OID"), "name": loc.get("Name"), "type": loc.get("LocationType"),
         # The CRF versions this site is on, newest effective first. Carried here
         # so the caller can check the model against the site as it is *now*,
         # without a second round trip.
         "versions": site_version_refs(root, loc.get("OID") or "", "")}
        for loc in root.findall(f".//{{{ODM}}}Location")
    ]


def assigned_version(sites: list[dict], number: str, name: str) -> tuple[str, str]:
    """The CRF version the given site is currently on, as (version OID, date).

    ("", "") when the site is unknown or carries no MetaDataVersionRef - the
    caller must be able to tell "no assignment" from "assigned to something
    else", because only the second is a disagreement worth shouting about.
    """
    for site in sites:
        if site.get("oid") in (number, name) or site.get("name") in (number, name):
            versions = site.get("versions") or []
            return versions[0] if versions else ("", "")
    return ("", "")


def _diagnose(error: str) -> str:
    """Turn Rave's terse AdminData rejections into something actionable.

    Rave reports a missing *permission* on administrative data as "Study does
    not exist", which is indistinguishable from a genuine typo unless you also
    know that clinical writes to the same study succeed.
    """
    text = (error or "").lower()
    if "study does not exist" in text:
        return (
            "Rave reports the study as missing for administrative data. Since clinical "
            "data posts to this same study succeed, this is almost certainly a missing "
            "permission rather than a wrong name: the account needs rights to upload "
            "administrative data (site creation). Create the site in the Rave UI, or "
            "point site.number at an existing site."
        )
    if "not a valid odm" in text:
        return "The payload failed schema validation before Rave looked at permissions."
    return ("If this is a permissions error, the account lacks site-creation rights - "
            "create the site manually in Rave and re-run.")


def ensure_site(
    client: RaveClient,
    config: Config,
    crf_version_oid: str,
    submitter: Submitter,
    sites: list[dict] | None = None,
) -> SiteState:
    """Verify the configured site exists; create it when allowed (FR-4.1, FR-4.2).

    `sites` lets a caller that has already listed them pass the result in rather
    than pay for a second round trip.
    """
    number = str(config.get("site.number"))
    name = str(config.get("site.name"))
    create_if_missing = bool(config.get("site.create_if_missing"))

    sites = list_sites(client, config) if sites is None else sites
    known = [s["oid"] for s in sites if s["oid"]]
    match = next((s for s in sites if s["oid"] == number or s["name"] == number), None)

    if match:
        log.info("site already exists", extra={"site": number})
        return SiteState(number=number, name=match["name"] or name, exists=True,
                         action="exists", detail=f"already registered as {match['oid']}",
                         known_sites=known)

    if not create_if_missing:
        return SiteState(
            number=number, name=name, action="missing",
            detail=("site does not exist and site.create_if_missing is false; "
                    "create it in Rave or enable creation"),
            known_sites=known,
        )

    odm = build_site_odm(
        study_oid=config.study_env,
        site_number=number,
        site_name=name,
        metadata_version_oid=crf_version_oid,
    )
    result = submitter.post(odm, label="create_site", archive_dir=Path("site"))

    if not result.ok:
        return SiteState(
            number=number, name=name, action="create_failed",
            detail=f"{result.error}. {_diagnose(result.error)}",
            known_sites=known,
        )

    if result.status == "DRY_RUN":
        return SiteState(number=number, name=name, action="created", created=False,
                         detail="dry run: AdminData payload written, not posted",
                         known_sites=known)

    # Confirm with Rave rather than trusting the acknowledgement.
    try:
        refreshed = list_sites(client, config)
    except RaveError as exc:
        return SiteState(number=number, name=name, created=True, exists=True,
                         action="created",
                         detail=f"accepted, but re-listing sites failed: {exc}",
                         known_sites=known)

    now_present = any(s["oid"] == number or s["name"] == number for s in refreshed)
    if not now_present:
        return SiteState(
            number=number, name=name, action="create_failed",
            detail=("Rave accepted the payload but the site is still not listed. "
                    "It may require activation for the study, or a CRF version "
                    "assignment, before it becomes usable."),
            known_sites=[s["oid"] for s in refreshed if s["oid"]],
        )

    log.info("site created", extra={"site": number})
    return SiteState(number=number, name=name, exists=True, created=True,
                     action="created", detail="created and confirmed present",
                     known_sites=[s["oid"] for s in refreshed if s["oid"]])
