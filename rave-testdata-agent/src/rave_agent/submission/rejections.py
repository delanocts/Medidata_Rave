"""Classify a Rave rejection using the rave-submission skill's table.

The marker-to-class table lives in
`.claude/skills/rave-submission/reference/error-codes.md` so a newly-encountered
rejection can be handled by editing that file, with no code change. A missing or
unreadable skill file degrades to a built-in table, so the pipeline still runs
from a checkout without `.claude/`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from ..utils.logging import get_logger

log = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RULES_FILE = (_REPO_ROOT / ".claude" / "skills" / "rave-submission"
               / "reference" / "error-codes.md")

_BLOCK = re.compile(r"<!-- BEGIN RULES -->(.*?)<!-- END RULES -->", re.DOTALL)

# Classes the submitter branches on.
TRANSIENT = "transient"
SHRINK_RECORDS = "shrink_records"
FOLDER_INACTIVE = "folder_inactive"
FORM_INACTIVE = "form_inactive"
DERIVED_FIELD = "derived_field"
BAD_VALUE = "bad_value"
PAYLOAD_SHAPE = "payload_shape"
PERMISSION = "permission"
SEMANTIC = "semantic"

# Order matters: the first matching marker wins, so specific before general.
_FALLBACK_RULES: list[tuple[str, str, str]] = [
    ("record restricted by max limit", SHRINK_RECORDS, "per-form log cap"),
    ("folder not found", FOLDER_INACTIVE, ""),
    ("form does not exist in the designated folder", FORM_INACTIVE, ""),
    ("transaction on derived field", DERIVED_FIELD, ""),
    ("data not in dictionary", BAD_VALUE, ""),
    ("record does not exist", PAYLOAD_SHAPE, ""),
    ("record already exists", PAYLOAD_SHAPE, ""),
    ("field does not exist", PAYLOAD_SHAPE, ""),
    ("study does not exist", PERMISSION, ""),
]


@lru_cache(maxsize=1)
def _rules() -> list[tuple[str, str, str]]:
    """Parse the skill's marker table, preserving its order."""
    if not _RULES_FILE.is_file():
        log.debug("rejection table absent; using built-in rules")
        return list(_FALLBACK_RULES)

    try:
        text = _RULES_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("rejection table unreadable", extra={"error": str(exc)})
        return list(_FALLBACK_RULES)

    match = _BLOCK.search(text)
    if not match:
        log.warning("rejection table has no BEGIN/END RULES block")
        return list(_FALLBACK_RULES)

    parsed: list[tuple[str, str, str]] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        marker, klass = _normalise(parts[0]), parts[1]
        note = parts[2] if len(parts) > 2 else ""
        parsed.append((marker, klass, note))

    if not parsed:
        return list(_FALLBACK_RULES)
    return parsed


def _normalise(text: str) -> str:
    """Lowercase and collapse runs of whitespace.

    Rave's messages are not consistently spaced - "Transaction on  derived
    field" carries a double space - so markers would otherwise miss.
    """
    return " ".join((text or "").lower().split())


def classify_rejection(reason: str) -> tuple[str, str]:
    """Return (class, note) for a Rave rejection string."""
    text = _normalise(reason)
    for marker, klass, note in _rules():
        if marker in text:
            return klass, note
    return SEMANTIC, ""


def is_class(reason: str, klass: str) -> bool:
    return classify_rejection(reason)[0] == klass
