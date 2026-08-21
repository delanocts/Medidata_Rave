"""Transient vs semantic failure classification (ERR-2, FR-7.4, FR-7.5).

A transient failure is worth retrying; a semantic one never is, and must be
surfaced with the offending detail intact.
"""
from __future__ import annotations

import requests

# Status codes that mean "try again later", not "your request was wrong".
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class RaveError(RuntimeError):
    """Base class for every failure reaching the caller from the Rave layer."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class TransientRaveError(RaveError):
    """Retryable: timeout, connection reset, 429, 5xx."""


class SemanticRaveError(RaveError):
    """Not retryable: bad request, rejected payload, missing object."""


class AuthError(SemanticRaveError):
    """401/403 - credentials wrong, or the account lacks the required role."""


class NotFoundError(SemanticRaveError):
    """404 - the study, site or subject is not visible to this account."""


def classify(exc: BaseException) -> RaveError:
    """Map a raw transport or rwslib exception onto our hierarchy."""
    if isinstance(exc, RaveError):
        return exc

    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return TransientRaveError(f"{type(exc).__name__}: {exc}")

    status = _status_of(exc)
    text = _body_of(exc)

    if status is not None:
        if status in (401, 403):
            return AuthError(
                f"HTTP {status}: authentication failed or the account lacks the required permission",
                status_code=status,
                detail=text,
            )
        if status == 404:
            return NotFoundError(f"HTTP {status}: not found", status_code=status, detail=text)
        if status in TRANSIENT_STATUS:
            return TransientRaveError(f"HTTP {status}", status_code=status, detail=text)
        return SemanticRaveError(f"HTTP {status}", status_code=status, detail=text)

    return SemanticRaveError(f"{type(exc).__name__}: {exc}", detail=text)


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    if response is not None and isinstance(getattr(response, "status_code", None), int):
        return response.status_code
    return None


def _body_of(exc: BaseException) -> str | None:
    """Best-effort extraction of the RWS reason text, truncated for logs."""
    for attr in ("rws_error", "errordescription", "reason_code"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)[:2000]
    response = getattr(exc, "response", None)
    if response is not None:
        text = getattr(response, "text", None)
        if text:
            return str(text)[:2000]
    message = str(exc)
    return message[:2000] if message else None
