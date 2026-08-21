"""Per-subject activation state, persisted per pass (FR-8.6).

Empty-but-active folders are invisible in the clinical dataset, so activation
cannot be observed by reading. It is established by *writing*: a folder that
accepts data is active, and one that refuses with a structural error is not.
That makes the submission itself the detector, and this file the record of what
each pass learned.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The folder itself is not part of the subject yet: no point trying its forms.
FOLDER_INACTIVE_MARKERS = (
    "folder not found",
    "folder does not exist",
    "does not exist for the subject",
)

# The folder exists but this particular form is not in it yet - typically it
# arrives with a matrix that has not merged. Other forms in the same folder may
# still be writable, so this must NOT abandon the folder.
FORM_INACTIVE_MARKERS = (
    "form does not exist in the designated folder",
    "form does not exist",
)


def is_folder_inactive(reason: str) -> bool:
    text = (reason or "").lower()
    return any(marker in text for marker in FOLDER_INACTIVE_MARKERS)


def is_form_inactive(reason: str) -> bool:
    text = (reason or "").lower()
    if is_folder_inactive(text):
        return False
    return any(marker in text for marker in FORM_INACTIVE_MARKERS)


def is_not_active(reason: str) -> bool:
    """Either kind of structural refusal, as opposed to a bad value."""
    return is_folder_inactive(reason) or is_form_inactive(reason)


@dataclass
class FolderState:
    folder_oid: str
    active: bool = False
    populated_forms: list[str] = field(default_factory=list)
    refused_forms: dict[str, str] = field(default_factory=dict)
    first_seen_pass: int | None = None


@dataclass
class ActivationState:
    subject_id: str
    study: str
    environment: str
    passes_run: int = 0
    folders: dict[str, FolderState] = field(default_factory=dict)
    predicted: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    def folder(self, folder_oid: str) -> FolderState:
        return self.folders.setdefault(folder_oid, FolderState(folder_oid=folder_oid))

    def mark_active(self, folder_oid: str, form_oid: str, pass_number: int) -> bool:
        """Record a successful write. Returns True if this is a new activation."""
        state = self.folder(folder_oid)
        newly = not state.active
        state.active = True
        if state.first_seen_pass is None:
            state.first_seen_pass = pass_number
        if form_oid not in state.populated_forms:
            state.populated_forms.append(form_oid)
        state.refused_forms.pop(form_oid, None)
        return newly

    def mark_refused(self, folder_oid: str, form_oid: str, reason: str) -> None:
        self.folder(folder_oid).refused_forms[form_oid] = reason

    @property
    def active_folders(self) -> list[str]:
        return sorted(oid for oid, s in self.folders.items() if s.active)

    def is_populated(self, folder_oid: str, form_oid: str) -> bool:
        """FR-8.4: never re-submit something already written."""
        state = self.folders.get(folder_oid)
        return bool(state and form_oid in state.populated_forms)

    def record_pass(self, pass_number: int, summary: dict) -> None:
        self.passes_run = max(self.passes_run, pass_number)
        self.history.append({
            "pass": pass_number,
            "at": datetime.now(timezone.utc).isoformat(),
            **summary,
        })

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "subject_id": self.subject_id,
            "study": self.study,
            "environment": self.environment,
            "passes_run": self.passes_run,
            "active_folders": self.active_folders,
            "predicted_folders": self.predicted,
            "never_activated": sorted(set(self.predicted) - set(self.active_folders)),
            "folders": {k: asdict(v) for k, v in sorted(self.folders.items())},
            "history": self.history,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path, subject_id: str, study: str, environment: str) -> "ActivationState":
        if not path.is_file():
            return cls(subject_id=subject_id, study=study, environment=environment)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(subject_id=subject_id, study=study, environment=environment)

        state = cls(
            subject_id=raw.get("subject_id", subject_id),
            study=raw.get("study", study),
            environment=raw.get("environment", environment),
            passes_run=raw.get("passes_run", 0),
            predicted=raw.get("predicted_folders") or [],
            history=raw.get("history") or [],
        )
        for oid, payload in (raw.get("folders") or {}).items():
            state.folders[oid] = FolderState(
                folder_oid=payload.get("folder_oid", oid),
                active=bool(payload.get("active")),
                populated_forms=list(payload.get("populated_forms") or []),
                refused_forms=dict(payload.get("refused_forms") or {}),
                first_seen_pass=payload.get("first_seen_pass"),
            )
        return state
