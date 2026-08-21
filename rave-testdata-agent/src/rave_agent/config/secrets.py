"""Secret loading and redaction (C-4, SEC-1, SEC-3).

Secrets come from .env or the process environment. They are registered with the
redactor the moment they are read, so any later attempt to log, serialise or
prompt with them is scrubbed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_REDACTED = "***REDACTED***"

# Values registered here are scrubbed from every log record and artifact.
_registry: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mark a value as secret. Short values are ignored to avoid mangling text."""
    if value and len(value) >= 4:
        _registry.add(value)


def redact(text: str) -> str:
    """Replace every registered secret occurrence in *text*."""
    if not text:
        return text
    for secret in _registry:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader. Does not overwrite values already in the environment."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


@dataclass(frozen=True)
class Secrets:
    rave_username: str
    rave_password: str
    anthropic_api_key: str

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    def __repr__(self) -> str:  # never leak via repr/traceback
        return f"Secrets(rave_username={self.rave_username!r}, rave_password={_REDACTED}, anthropic_api_key={_REDACTED})"

    __str__ = __repr__


class MissingSecretError(RuntimeError):
    pass


def load_secrets(env_file: Path | None = None, require_anthropic: bool = True) -> Secrets:
    """Load secrets from .env then the environment, and register them for redaction."""
    file_values = load_dotenv(env_file) if env_file else {}

    def pick(key: str) -> str:
        return (os.environ.get(key) or file_values.get(key) or "").strip()

    username = pick("RAVE_USERNAME")
    password = pick("RAVE_PASSWORD")
    api_key = pick("ANTHROPIC_API_KEY")

    missing = [k for k, v in (("RAVE_USERNAME", username), ("RAVE_PASSWORD", password)) if not v]
    if require_anthropic and not api_key:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        where = f" (looked in {env_file} and the environment)" if env_file else ""
        raise MissingSecretError(
            "Missing required secret(s): " + ", ".join(missing) + where
        )

    for value in (password, api_key):
        register_secret(value)

    return Secrets(rave_username=username, rave_password=password, anthropic_api_key=api_key)
