#!/usr/bin/env bash
# PostToolUse check: warn when generated ODM is not well-formed XML.
#
# Advisory only - it reports and exits 0, because a half-written file during an
# edit is normal and should not fail the tool call. Schema validation before a
# POST is the submitter's job (FR-7.2); this is a fast structural smell test.
set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"

[ -z "${path:-}" ] && exit 0
case "$path" in *.xml) ;; *) exit 0 ;; esac
[ -f "$path" ] || exit 0

if command -v python >/dev/null 2>&1; then
  python - "$path" <<'PY' || true
import sys
from xml.etree import ElementTree as ET
path = sys.argv[1]
try:
    with open(path, "rb") as handle:
        data = handle.read()
    # Strip a UTF-8 BOM: Rave emits one and it is not a parse error here.
    ET.fromstring(data[3:] if data[:3] == b"\xef\xbb\xbf" else data)
except Exception as exc:  # noqa: BLE001 - advisory only
    print(f"ODM check: {path} is not well-formed XML - {exc}", file=sys.stderr)
PY
fi

exit 0
