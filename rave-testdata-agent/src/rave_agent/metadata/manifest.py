"""Metadata manifest - traceability for every downloaded artifact (FR-2.6)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ArtifactRecord:
    name: str
    filename: str
    source_url: str
    retrieved_at: str
    sha256: str
    bytes: int
    study_version: str | None = None
    acquisition: str = "rws"          # rws | manual
    note: str = ""


@dataclass
class MetadataManifest:
    study: str
    environment: str
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path, study: str, environment: str) -> "MetadataManifest":
        if not path.is_file():
            return cls(study=study, environment=environment)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(study=study, environment=environment)
        manifest = cls(
            study=raw.get("study", study),
            environment=raw.get("environment", environment),
            created_at=raw.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=raw.get("updated_at", ""),
        )
        for name, rec in (raw.get("artifacts") or {}).items():
            try:
                manifest.artifacts[name] = ArtifactRecord(**rec)
            except TypeError:
                continue  # schema drift: treat as absent, forcing a re-download
        return manifest

    def record(self, artifact: ArtifactRecord) -> None:
        self.artifacts[artifact.name] = artifact

    def get(self, name: str) -> ArtifactRecord | None:
        return self.artifacts.get(name)

    def is_fresh(self, name: str, directory: Path) -> bool:
        """True when the artifact exists on disk and still matches its hash (FR-2.5)."""
        record = self.artifacts.get(name)
        if record is None:
            return False
        path = directory / record.filename
        if not path.is_file():
            return False
        return sha256_file(path) == record.sha256

    def save(self, path: Path) -> Path:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "study": self.study,
            "environment": self.environment,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "artifacts": {k: asdict(v) for k, v in self.artifacts.items()},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
