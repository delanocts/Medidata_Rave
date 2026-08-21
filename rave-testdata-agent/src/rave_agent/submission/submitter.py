"""POST ODM to Rave and interpret the response (FR-7.3 - FR-7.8).

Every request and response is archived before and after the call, so the audit
trail survives even when the call fails (SEC-6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rwslib.rws_requests import PostDataRequest

from ..rave.client import RaveClient
from ..rave.errors import RaveError, SemanticRaveError, TransientRaveError
from ..utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class SubmissionResult:
    ok: bool
    label: str
    request_path: Path | None = None
    response_path: Path | None = None
    status: str = ""
    reason: str = ""
    subjects_touched: int = 0
    raw_response: str = ""
    error: str = ""
    transient: bool = False
    details: dict = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return f"{self.label}: accepted ({self.status})"
        return f"{self.label}: REJECTED - {self.error or self.reason}"


class Submitter:
    """Serialised writer for one study. Submissions per subject must not overlap (C-5)."""

    def __init__(self, client: RaveClient, archive_root: Path, dry_run: bool = False):
        self.client = client
        self.archive_root = archive_root
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    def _archive(self, folder: Path, name: str, payload: bytes | str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        path.write_bytes(data)
        return path

    # ------------------------------------------------------------------
    def post(self, odm_bytes: bytes, label: str, archive_dir: Path) -> SubmissionResult:
        """Post one ODM document. `--dry-run` writes the payload and stops (FR-7.8)."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")[:-3]
        folder = self.archive_root / archive_dir
        request_path = self._archive(folder, f"{stamp}_{label}_request.xml", odm_bytes)

        if self.dry_run:
            log.info("dry run - not posted", extra={"label": label, "path": str(request_path)})
            return SubmissionResult(
                ok=True, label=label, request_path=request_path,
                status="DRY_RUN", reason="payload written, nothing sent",
            )

        try:
            result = self.client.send(PostDataRequest(odm_bytes), label=label)
        except (SemanticRaveError, TransientRaveError, RaveError) as exc:
            body = getattr(exc, "detail", None) or str(exc)
            response_path = self._archive(folder, f"{stamp}_{label}_response.xml", body)
            transient = isinstance(exc, TransientRaveError)
            log.error("submission failed", extra={
                "label": label, "error": str(exc), "transient": transient})
            return SubmissionResult(
                ok=False, label=label, request_path=request_path,
                response_path=response_path, error=str(exc),
                raw_response=body, transient=transient,
            )

        response = result.value
        raw = str(response)
        response_path = self._archive(folder, f"{stamp}_{label}_response.xml", raw)

        parsed = _interpret(response)
        log.info("submission accepted", extra={
            "label": label, "correlation_id": result.correlation_id, **parsed})
        return SubmissionResult(
            ok=True, label=label, request_path=request_path,
            response_path=response_path, raw_response=raw,
            status=parsed.get("status", ""), reason=parsed.get("reason", ""),
            subjects_touched=parsed.get("subjects_touched", 0), details=parsed,
        )


def _interpret(response) -> dict:
    """Pull the useful fields off an RWSPostResponse without assuming they exist."""
    out: dict = {}
    for attribute, key in (
        ("istransactionsuccessful", "successful"),
        ("transaction_id", "transaction_id"),
        ("subjects_touched", "subjects_touched"),
        ("forms_touched", "forms_touched"),
        ("fields_touched", "fields_touched"),
        ("logs_touched", "logs_touched"),
        ("rws_error", "reason"),
        ("errordescription", "reason"),
        ("reason_code", "reason_code"),
    ):
        value = getattr(response, attribute, None)
        if value not in (None, ""):
            out[key] = value
    out["status"] = "SUCCESS" if out.get("successful") else out.get("status", "SEE_RESPONSE")
    return out
