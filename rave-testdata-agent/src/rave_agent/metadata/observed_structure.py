"""Learn real study structure by observing existing subjects (read-only).

The version metadata only declares form assignments for the default matrix's
folders. Folders reachable through other matrices are named but empty, so the
generator would have nothing to fill for them.

Sampling subjects that already exist recovers that mapping from what Rave itself
produced. This reads clinical data and never writes; the values are ignored
entirely - only the folder/form/item-group shape is kept.
"""
from __future__ import annotations

import collections
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config.loader import Config
from ..rave.client import RaveClient
from ..rave.errors import RaveError
from ..utils.logging import get_logger
from ..utils.xml import parse_xml

log = get_logger(__name__)

ODM = "http://www.cdisc.org/ns/odm/v1.3"


@dataclass
class ObservedStructure:
    study: str
    environment: str
    crf_version_oid: str = ""
    subjects_sampled: list[str] = field(default_factory=list)
    subjects_failed: dict[str, str] = field(default_factory=dict)
    # folder OID -> form OID -> number of instances seen
    folder_forms: dict[str, dict[str, int]] = field(default_factory=dict)
    # form OID -> item group OID -> max records seen on one form instance
    form_group_max_records: dict[str, dict[str, int]] = field(default_factory=dict)
    sampled_at: str = ""

    @property
    def folders(self) -> list[str]:
        return sorted(self.folder_forms)

    def forms_in(self, folder_oid: str) -> list[str]:
        return sorted(self.folder_forms.get(folder_oid, {}))

    def to_dict(self) -> dict:
        return {
            "study": self.study,
            "environment": self.environment,
            "crf_version_oid": self.crf_version_oid,
            "sampled_at": self.sampled_at,
            "subjects_sampled": self.subjects_sampled,
            "subjects_failed": self.subjects_failed,
            "folder_count": len(self.folder_forms),
            "folder_forms": {k: dict(sorted(v.items())) for k, v in sorted(self.folder_forms.items())},
            "form_group_max_records": self.form_group_max_records,
            "note": "Observed from existing subjects, read-only. Structure only; no values retained.",
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "ObservedStructure | None":
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        observed = cls(
            study=raw.get("study", ""),
            environment=raw.get("environment", ""),
            crf_version_oid=raw.get("crf_version_oid", ""),
            subjects_sampled=raw.get("subjects_sampled", []),
            subjects_failed=raw.get("subjects_failed", {}),
            sampled_at=raw.get("sampled_at", ""),
        )
        observed.folder_forms = {k: dict(v) for k, v in (raw.get("folder_forms") or {}).items()}
        observed.form_group_max_records = {
            k: dict(v) for k, v in (raw.get("form_group_max_records") or {}).items()
        }
        return observed


def pick_subjects(subjects_xml: Path, limit: int) -> list[str]:
    """Choose subjects most likely to reveal structure.

    The subject list carries workflow flags. A subject marked Empty="Yes" has no
    data to learn from, and Touched="No" means nothing was ever entered, so both
    are deprioritised. Within each tier the picks are spread across the list
    rather than clustered at the start, since subjects tend to be grouped by site.
    """
    root = parse_xml(subjects_xml.read_bytes())
    rich: list[str] = []
    sparse: list[str] = []
    seen: set[str] = set()

    for node in root.findall(f".//{{{ODM}}}SubjectData"):
        key = node.get("SubjectKey")
        if not key or key in seen or key.strip() in ("", "0"):
            continue
        seen.add(key)
        has_data = (node.get("Empty") or "").lower() != "yes"
        touched = (node.get("Touched") or "").lower() != "no"
        (rich if (has_data and touched) else sparse).append(key)

    def spread(items: list[str], count: int) -> list[str]:
        if count <= 0 or not items:
            return []
        if len(items) <= count:
            return list(items)
        step = len(items) / count
        return [items[int(i * step)] for i in range(count)]

    chosen = spread(rich, limit)
    if len(chosen) < limit:
        chosen += spread(sparse, limit - len(chosen))
    return chosen


def sample_subjects(
    client: RaveClient,
    config: Config,
    subject_keys: list[str],
    crf_version_oid: str = "",
) -> ObservedStructure:
    """Fetch each subject's dataset and record only its structural shape."""
    from rwslib.rws_requests import SubjectDatasetRequest

    observed = ObservedStructure(
        study=config.study_name,
        environment=config.environment,
        crf_version_oid=crf_version_oid,
        sampled_at=datetime.now(timezone.utc).isoformat(),
    )
    folder_forms: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    group_max: dict[str, dict[str, int]] = collections.defaultdict(dict)

    for key in subject_keys:
        try:
            payload = client.send(
                SubjectDatasetRequest(config.study_name, config.environment, key)
            ).value
        except RaveError as exc:
            observed.subjects_failed[key] = str(exc)
            log.warning("subject sample failed", extra={"subject": key, "error": str(exc)})
            continue

        try:
            root = parse_xml(payload)
        except Exception as exc:  # noqa: BLE001
            observed.subjects_failed[key] = f"unparseable: {exc}"
            continue

        for event in root.findall(f".//{{{ODM}}}StudyEventData"):
            folder_oid = event.get("StudyEventOID")
            if not folder_oid:
                continue
            for form in event.findall(f"{{{ODM}}}FormData"):
                form_oid = form.get("FormOID")
                if not form_oid:
                    continue
                folder_forms[folder_oid][form_oid] += 1

                counts: collections.Counter = collections.Counter()
                for group in form.findall(f"{{{ODM}}}ItemGroupData"):
                    group_oid = group.get("ItemGroupOID")
                    if group_oid:
                        counts[group_oid] += 1
                for group_oid, count in counts.items():
                    previous = group_max[form_oid].get(group_oid, 0)
                    group_max[form_oid][group_oid] = max(previous, count)

        observed.subjects_sampled.append(key)
        log.info("subject sampled", extra={"subject": key, "folders": len(folder_forms)})

    observed.folder_forms = {k: dict(v) for k, v in folder_forms.items()}
    observed.form_group_max_records = {k: dict(v) for k, v in group_max.items()}
    return observed
