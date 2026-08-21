#!/usr/bin/env bash
# PreToolUse guardrail: stop a command that would expose .env or a credential.
#
# The runtime redacts secrets from logs and reports (CFG-3, SEC-3). This catches
# the other direction - a command that would print, copy or commit the file
# before any redaction can apply.
#
# Exit 2 blocks the tool call; anything else lets it through.
set -uo pipefail

payload="$(cat)"

# The hook receives a JSON envelope. Extract the command, so trailing quotes and
# braces in the raw JSON cannot defeat an end-of-string anchor.
command="$(printf '%s' "$payload" | python -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
found = data.get("command") or (data.get("tool_input") or {}).get("command") or ""
print(found)
' 2>/dev/null)"
[ -z "${command:-}" ] && command="$payload"
command="$(printf '%s' "$command" | tr '[:upper:]' '[:lower:]')"

# Boundary after a filename: whitespace, end of string, or a JSON delimiter.
END='([[:space:]]|["}]|$)'

# Printing .env to the transcript.
if printf '%s' "$command" | grep -Eq "(^|[[:space:]])(cat|type|less|more|head|tail|strings|xxd)[[:space:]]+([^|;]*[[:space:]/])?\.env${END}"; then
  echo "BLOCKED: that would print .env to the transcript." >&2
  echo "Secrets must never appear in output (SEC-3). Read key names only, e.g.:" >&2
  echo "  sed 's/=.*/=<redacted>/' .env" >&2
  exit 2
fi

# Staging or committing it.
if printf '%s' "$command" | grep -Eq "git[[:space:]]+(add|commit)[^|;]*\.env${END}"; then
  echo "BLOCKED: .env must never be committed (SEC-1)." >&2
  echo "It is git-ignored; keep it that way." >&2
  exit 2
fi

# A credential inline on the command line lands in shell history and logs.
if printf '%s' "$command" | grep -Eq "(rave_password|rave_username|anthropic_api_key)=[^[:space:]\"']{6,}"; then
  echo "BLOCKED: that puts a credential on the command line, where it is logged." >&2
  echo "Put it in .env instead; the loader reads it from there (C-4)." >&2
  exit 2
fi

exit 0
