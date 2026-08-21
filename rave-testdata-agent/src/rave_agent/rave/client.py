"""The single choke point for every call into Rave.

Nothing else in this codebase may talk to RWS directly. Everything here is
rate-limited, retried only when the failure is transient, and logged with a
correlation id and full secret redaction.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests
from rwslib import RWSConnection
from rwslib.rws_requests import RWSRequest

from ..config.loader import Config
from ..config.secrets import Secrets
from ..utils.logging import get_logger
from .errors import RaveError, TransientRaveError, classify
from .rate_limit import RateLimiter

log = get_logger(__name__)


@dataclass
class CallResult:
    """What a single RWS round trip produced, for the audit trail (SEC-6)."""
    value: Any
    correlation_id: str
    attempts: int
    elapsed_seconds: float
    url_path: str


class RaveClient:
    """Wraps rwslib's connection with retry, rate limiting and redacted logging."""

    def __init__(self, config: Config, secrets: Secrets):
        self.config = config
        rave = config.data["rave"]

        if not config.base_url.startswith("https://"):
            raise ValueError(f"base_url must be https (SEC-2), got {config.base_url!r}")
        if rave.get("verify_tls") is not True:
            raise ValueError("TLS verification cannot be disabled (SEC-2)")

        virtual_dir = rave["rws_path"].strip("/")
        self._conn = RWSConnection(
            domain=config.base_url,
            username=secrets.rave_username,
            password=secrets.rave_password,
            virtual_dir=virtual_dir,
        )
        self.timeout = rave.get("timeout_seconds", 60)
        self.max_retries = rave.get("max_retries", 3)
        self.backoff = rave.get("retry_backoff_seconds", 2)
        self.limiter = RateLimiter(rave.get("requests_per_minute", 30))
        self.base_url = self._conn.base_url

    # ------------------------------------------------------------------
    def send(self, request: RWSRequest, *, label: str | None = None) -> CallResult:
        """Send one RWS request, retrying transient failures only (ERR-2)."""
        correlation_id = uuid.uuid4().hex[:12]
        name = label or type(request).__name__
        try:
            url_path = request.url_path()
        except Exception:  # url construction is part of the request contract
            url_path = "<unavailable>"

        started = time.monotonic()
        last: RaveError | None = None

        for attempt in range(1, self.max_retries + 2):
            waited = self.limiter.acquire()
            if waited:
                log.debug("rate limited", extra={"correlation_id": correlation_id, "waited_s": round(waited, 2)})
            try:
                # rwslib does its own connection-level retry; we drive retries here
                # so that only transient failures are ever repeated.
                value = self._conn.send_request(request, timeout=self.timeout, retries=1)
                elapsed = time.monotonic() - started
                log.info(
                    "rws call ok",
                    extra={
                        "correlation_id": correlation_id,
                        "request": name,
                        "url_path": url_path,
                        "attempts": attempt,
                        "elapsed_s": round(elapsed, 3),
                    },
                )
                return CallResult(value, correlation_id, attempt, elapsed, url_path)
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                last = classify(exc)
                retryable = isinstance(last, TransientRaveError) and attempt <= self.max_retries
                log.warning(
                    "rws call failed",
                    extra={
                        "correlation_id": correlation_id,
                        "request": name,
                        "url_path": url_path,
                        "attempt": attempt,
                        "error": str(last),
                        "status_code": last.status_code,
                        "retrying": retryable,
                    },
                )
                if not retryable:
                    raise last from exc
                time.sleep(self.backoff * (2 ** (attempt - 1)))

        raise last if last else RaveError("request failed with no recorded error")

    # ------------------------------------------------------------------
    def get_raw(self, path: str) -> requests.Response:
        """Unauthenticated GET against an RWS path, for reachability probes only.

        Used by the connection test to hit endpoints that need no credentials.
        Everything carrying data must go through :meth:`send`.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        self.limiter.acquire()
        return requests.get(url, timeout=self.timeout, verify=True)
