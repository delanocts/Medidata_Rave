"""A2 - Metadata Acquisition Agent (FR-2).

Downloads everything A3 needs to build the study model, skips artifacts that are
already cached and unchanged, and records provenance for each one.

Nothing study-specific is hardcoded: every identifier comes from config or from
metadata already retrieved.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config.loader import Config
from ..rave.client import RaveClient
from ..rave.errors import RaveError
from ..utils.logging import get_logger
from ..utils.xml import parse_xml_file, to_bytes
from .manifest import ArtifactRecord, MetadataManifest, sha256_bytes, sha256_file
from .observed_structure import ObservedStructure, pick_subjects, sample_subjects

log = get_logger(__name__)

ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"
MDSOL_NS = "http://www.mdsol.com/ns/odm/metadata"

# Extensions accepted when looking for a manually exported ALS workbook (FR-2.2).
ALS_SUFFIXES = (".xlsx", ".xlsm", ".xls")   # Architect exports SpreadsheetML named .xls


@dataclass
class DownloadOutcome:
    name: str
    status: str          # downloaded | cached | manual | missing | failed
    filename: str = ""
    bytes: int = 0
    detail: str = ""


class MetadataAcquisition:
    def __init__(self, client: RaveClient, config: Config):
        self.client = client
        self.config = config
        self.dir = config.study_output_dir / "metadata"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "metadata_manifest.json"
        self.manifest = MetadataManifest.load(
            self.manifest_path, config.study_name, config.environment
        )
        self.outcomes: list[DownloadOutcome] = []

    # ------------------------------------------------------------------
    def _store(
        self,
        name: str,
        filename: str,
        fetch: Callable[[], tuple[str | bytes, str]],
        force: bool,
        study_version: str | None = None,
    ) -> DownloadOutcome:
        """Fetch and persist one artifact unless a fresh copy is already cached."""
        if not force and self.manifest.is_fresh(name, self.dir):
            record = self.manifest.get(name)
            outcome = DownloadOutcome(name, "cached", record.filename, record.bytes,
                                      "hash matches manifest, download skipped")
            log.info("cache skip", extra={"artifact": name, "file": record.filename})
            self.outcomes.append(outcome)
            return outcome

        try:
            payload, url = fetch()
        except RaveError as exc:
            outcome = DownloadOutcome(name, "failed", filename, 0, str(exc))
            log.error("download failed", extra={"artifact": name, "error": str(exc)})
            self.outcomes.append(outcome)
            return outcome

        # to_bytes repairs rwslib's latin-1 mis-decode and strips any BOM.
        data = to_bytes(payload)
        path = self.dir / filename
        path.write_bytes(data)

        self.manifest.record(ArtifactRecord(
            name=name,
            filename=filename,
            source_url=url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            sha256=sha256_bytes(data),
            bytes=len(data),
            study_version=study_version,
            acquisition="rws",
        ))
        outcome = DownloadOutcome(name, "downloaded", filename, len(data))
        log.info("downloaded", extra={"artifact": name, "file": filename, "bytes": len(data)})
        self.outcomes.append(outcome)
        return outcome

    def _url_for(self, request) -> str:
        try:
            return f"{self.client.base_url}/{request.url_path()}"
        except Exception:
            return f"{self.client.base_url}/<unavailable>"

    # ------------------------------------------------------------------
    def resolve_version(self) -> tuple[str, str]:
        """Pick the CRF version to use: config pin, else newest (OQ-5)."""
        from rwslib.rws_requests import StudyVersionsRequest

        request = StudyVersionsRequest(self.config.study_name)
        versions = self.client.send(request).value
        listed = [{"oid": v.oid, "name": v.name} for v in versions]
        if not listed:
            raise RaveError(f"no CRF versions published for {self.config.study_name}")

        self._versions = listed
        pinned = self.config.get("study.crf_version")
        if pinned is not None:
            match = next((v for v in listed if str(v["oid"]) == str(pinned)), None)
            if match is None:
                raise RaveError(
                    f"study.crf_version {pinned!r} not among published versions "
                    f"{[v['oid'] for v in listed]}"
                )
            return str(match["oid"]), str(match["name"])

        # Newest first, as returned by RWS.
        return str(listed[0]["oid"]), str(listed[0]["name"])

    # ------------------------------------------------------------------
    def run(self, force: bool = False) -> dict:
        from rwslib.rws_requests import (
            StudyDraftsRequest,
            StudySubjectsRequest,
            StudyVersionRequest,
            StudyVersionsRequest,
        )
        from rwslib.rws_requests.odm_adapter import SitesMetadataRequest, VersionFoldersRequest

        study = self.config.study_name
        env = self.config.environment

        version_oid, version_name = self.resolve_version()
        log.info("crf version selected",
                 extra={"version_oid": version_oid, "version_name": version_name})

        # 1. Study versions list (FR-2.3)
        def fetch_versions():
            request = StudyVersionsRequest(study)
            self.client.send(request)
            return json.dumps(self._versions, indent=2), self._url_for(request)

        self._store("study_versions", "study_versions.json", fetch_versions, force)

        # 2. Drafts list - drafts cannot be downloaded on Rave 1.16.0, but knowing
        #    they exist tells the user what to export as ALS (FR-2.2).
        def fetch_drafts():
            request = StudyDraftsRequest(study)
            drafts = self.client.send(request).value
            payload = [{"oid": d.oid, "name": d.name} for d in drafts]
            return json.dumps(payload, indent=2), self._url_for(request)

        self._store("study_drafts", "study_drafts.json", fetch_drafts, force)

        # 3. The ODM study metadata for the selected version (FR-2.1)
        def fetch_metadata():
            request = StudyVersionRequest(study, version_oid)
            return self.client.send(request).value, self._url_for(request)

        self._store("odm_metadata", f"{study}_{version_oid}_metadata.xml",
                    fetch_metadata, force, study_version=version_oid)

        # 4. Matrix / folder assignments - the only place the full matrix map lives
        def fetch_folders():
            request = VersionFoldersRequest(study, env)
            return self.client.send(request).value, self._url_for(request)

        self._store("version_folders", "version_folders.xml", fetch_folders, force,
                    study_version=version_oid)

        # 5. Sites (FR-2.3)
        def fetch_sites():
            request = SitesMetadataRequest(study, env)
            return self.client.send(request).value, self._url_for(request)

        self._store("sites", "sites.xml", fetch_sites, force)

        # 6. Subjects (FR-2.3, feeds FR-5.2 collision policy)
        def fetch_subjects():
            request = StudySubjectsRequest(study, env, status=True)
            # RWSSubjects renders back to its source ODM via __str__.
            return str(self.client.send(request).value), self._url_for(request)

        self._store("subjects", "subjects.xml", fetch_subjects, force)

        # 7. Observed structure - learn folder/form shape from existing subjects.
        #    Read-only; recovers assignments the version metadata does not declare.
        self._sample_existing_subjects(version_oid, force)

        # 8. ALS workbook - manual acquisition only on this instance (FR-2.2)
        self._register_manual_als()

        self.manifest.save(self.manifest_path)
        return self.summary(version_oid, version_name)

    # ------------------------------------------------------------------
    def _sample_existing_subjects(self, version_oid: str, force: bool) -> DownloadOutcome:
        """Observe existing subjects to recover undeclared folder/form assignments."""
        limit = int(self.config.get("metadata.sample_subjects", 6) or 0)
        target = self.dir / "observed_structure.json"

        if limit <= 0:
            outcome = DownloadOutcome("observed_structure", "missing", "", 0,
                                      "metadata.sample_subjects is 0; sampling disabled")
            self.outcomes.append(outcome)
            return outcome

        if not force and target.is_file():
            existing = ObservedStructure.load(target)
            if existing and existing.crf_version_oid == version_oid:
                outcome = DownloadOutcome(
                    "observed_structure", "cached", target.name, target.stat().st_size,
                    f"{len(existing.folder_forms)} folder(s) from "
                    f"{len(existing.subjects_sampled)} subject(s)")
                log.info("cache skip", extra={"artifact": "observed_structure"})
                self.outcomes.append(outcome)
                return outcome

        subjects_record = self.manifest.get("subjects")
        if subjects_record is None:
            outcome = DownloadOutcome("observed_structure", "failed", "", 0,
                                      "subject list unavailable")
            self.outcomes.append(outcome)
            return outcome

        keys = pick_subjects(self.dir / subjects_record.filename, limit)
        if not keys:
            outcome = DownloadOutcome("observed_structure", "missing", "", 0,
                                      "study has no existing subjects to learn from")
            self.outcomes.append(outcome)
            return outcome

        observed = sample_subjects(self.client, self.config, keys, version_oid)
        observed.save(target)
        outcome = DownloadOutcome(
            "observed_structure", "downloaded", target.name, target.stat().st_size,
            f"{len(observed.folder_forms)} folder(s) from "
            f"{len(observed.subjects_sampled)} subject(s)")
        self.outcomes.append(outcome)
        return outcome

    # ------------------------------------------------------------------
    def _register_manual_als(self) -> DownloadOutcome:
        """Pick up a user-placed ALS workbook, logging that acquisition was manual."""
        candidates = sorted(
            p for p in self.dir.iterdir()
            if p.is_file() and p.suffix.lower() in ALS_SUFFIXES and not p.name.startswith("~$")
        )
        if not candidates:
            outcome = DownloadOutcome(
                "als", "missing", "", 0,
                "no ALS workbook present. RWS on this instance cannot export one; "
                "place it here manually to enable dynamics analysis.",
            )
            log.warning("ALS not present", extra={"searched": str(self.dir)})
            self.outcomes.append(outcome)
            return outcome

        chosen = max(candidates, key=lambda p: p.stat().st_mtime)
        digest = sha256_file(chosen)
        existing = self.manifest.get("als")
        status = "cached" if existing and existing.sha256 == digest else "manual"

        self.manifest.record(ArtifactRecord(
            name="als",
            filename=chosen.name,
            source_url="(manual export from Rave Architect UI)",
            retrieved_at=datetime.now(timezone.utc).isoformat() if status == "manual"
            else existing.retrieved_at,
            sha256=digest,
            bytes=chosen.stat().st_size,
            acquisition="manual",
            note="Architect draft export; RWS provides no programmatic route (FR-2.2)",
        ))
        outcome = DownloadOutcome("als", status, chosen.name, chosen.stat().st_size,
                                  "manually exported ALS workbook")
        log.info("ALS registered", extra={"file": chosen.name, "status": status})
        self.outcomes.append(outcome)
        return outcome

    # ------------------------------------------------------------------
    def summary(self, version_oid: str, version_name: str) -> dict:
        """Structural counts, so a run's scope is visible without opening the XML."""
        stats: dict = {}
        metadata_record = self.manifest.get("odm_metadata")
        if metadata_record:
            path = self.dir / metadata_record.filename
            if path.is_file():
                root = parse_xml_file(path)
                stats["forms"] = len(root.findall(f".//{{{ODM_NS}}}FormDef"))
                stats["fields"] = len(root.findall(f".//{{{ODM_NS}}}ItemDef"))
                stats["item_groups"] = len(root.findall(f".//{{{ODM_NS}}}ItemGroupDef"))
                stats["codelists"] = len(root.findall(f".//{{{ODM_NS}}}CodeList"))
                stats["seed_folders"] = len(root.findall(f".//{{{ODM_NS}}}StudyEventDef"))
                stats["edit_checks"] = len(root.findall(f".//{{{MDSOL_NS}}}EditCheckDef"))
                stats["derivations"] = len(root.findall(f".//{{{MDSOL_NS}}}DerivationDef"))
                mdv = root.find(f".//{{{ODM_NS}}}MetaDataVersion")
                if mdv is not None:
                    stats["primary_form_oid"] = mdv.get(f"{{{MDSOL_NS}}}PrimaryFormOID")
                    stats["default_matrix_oid"] = mdv.get(f"{{{MDSOL_NS}}}DefaultMatrixOID")

        folders_record = self.manifest.get("version_folders")
        if folders_record:
            path = self.dir / folders_record.filename
            if path.is_file():
                root = parse_xml_file(path)
                matrices, all_folders = set(), set()
                for mdv in root.findall(f".//{{{ODM_NS}}}MetaDataVersion"):
                    if mdv.get("OID") != version_oid:
                        continue
                    matrices.add(mdv.get(f"{{{MDSOL_NS}}}MatrixOID") or "(default)")
                    for ref in mdv.findall(f".//{{{ODM_NS}}}StudyEventRef"):
                        all_folders.add(ref.get("StudyEventOID"))
                stats["matrices"] = len(matrices)
                stats["total_folders"] = len(all_folders)

        return {
            "study": self.config.study_name,
            "environment": self.config.environment,
            "crf_version": {"oid": version_oid, "name": version_name},
            "directory": str(self.dir),
            "artifacts": [o.__dict__ for o in self.outcomes],
            "structure": stats,
        }
