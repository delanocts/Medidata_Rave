"""XML helpers.

Two RWS quirks make a shared parse path necessary:

1. Some payloads carry a UTF-8 BOM *and* an ``encoding="utf-8"`` declaration,
   which lxml rejects.
2. rwslib does not set ``response.encoding`` on the dataset endpoints, so
   requests falls back to latin-1 and the UTF-8 bytes come back mis-decoded -
   the BOM arrives as three characters (U+00EF U+00BB U+00BF) rather than
   U+FEFF, and every other non-ASCII byte is mangled the same way. Re-encoding
   such a string with latin-1 recovers the original bytes exactly.

Every parse in this codebase goes through here rather than calling
etree.fromstring directly.
"""
from __future__ import annotations

from pathlib import Path

from lxml import etree

_BOM_BYTES = b"\xef\xbb\xbf"
_BOM_TEXT = "﻿"
# The UTF-8 BOM as it looks after being decoded with latin-1/cp1252.
_BOM_MOJIBAKE = "ï»¿"


def strip_bom(data: bytes) -> bytes:
    return data[len(_BOM_BYTES):] if data.startswith(_BOM_BYTES) else data


def to_bytes(payload: str | bytes) -> bytes:
    """Normalise an RWS payload to BOM-free UTF-8 bytes."""
    if isinstance(payload, bytes):
        return strip_bom(payload).lstrip()

    if payload.startswith(_BOM_MOJIBAKE):
        # Latin-1 mis-decode: round-trip back to the bytes RWS actually sent.
        try:
            return strip_bom(payload.encode("latin-1")).lstrip()
        except UnicodeEncodeError:
            # Mixed decoding; drop the stray BOM characters and keep the text.
            payload = payload[len(_BOM_MOJIBAKE):]

    return payload.lstrip(_BOM_TEXT).lstrip().encode("utf-8")


def parse_xml(payload: str | bytes):
    """Parse an RWS XML payload into an lxml element."""
    return etree.fromstring(to_bytes(payload))


def parse_xml_file(path: Path):
    return parse_xml(path.read_bytes())
